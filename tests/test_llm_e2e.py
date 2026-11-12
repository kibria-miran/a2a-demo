"""End-to-end negotiation with the LLM proposer on BOTH sides (S17-adjacent).

Scripts FakeLLM responses on both sides so every proposal validates. The
test asserts the negotiation completes and that the trace records
`source == "llm"` for outbound messages from both agents (proving neither
agent used its fallback mid-run).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import FakeLLM, loopback_client

from src import trace
from src.buyer_cli import negotiate
from src.client import A2AClient
from src.server import create_app
from src.store import Store
from src.utility import Profile

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]

SELLER_RESPONSES = [
    json.dumps({"terms": {"price": 1150, "delivery_days": 19, "revisions": 1},
                "rationale": "At 1150 we can keep the schedule."}),
    json.dumps({"terms": {"price": 1150, "delivery_days": 19, "revisions": 1},
                "rationale": "Holding position."}),
    json.dumps({"terms": {"price": 1100, "delivery_days": 18, "revisions": 2},
                "rationale": "Small move."}),
    json.dumps({"terms": {"price": 1000, "delivery_days": 18, "revisions": 4},
                "rationale": "Final offer."}),
]

BUYER_RESPONSES = [
    json.dumps({"rationale": "Happy to discuss terms."}),
    json.dumps({"terms": {"price": 650, "delivery_days": 10, "revisions": 5},
                "rationale": "Meet near the middle."}),
    json.dumps({"terms": {"price": 750, "delivery_days": 10, "revisions": 5},
                "rationale": "A step up."}),
    json.dumps({"terms": {"price": 750, "delivery_days": 20, "revisions": 3},
                "rationale": "Narrowing the gap."}),
]


def load_profile(name: str) -> Profile:
    return Profile.load(json.loads((ROOT / "profiles" / f"{name}.json").read_text()))


def read_events(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def test_full_negotiation_with_llm_proposer_on_both_sides(tmp_path: Any) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace.configure(trace_path)

    seller_store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    seller_chat = FakeLLM(SELLER_RESPONSES)
    buyer_chat = FakeLLM(BUYER_RESPONSES)
    app = create_app(
        seller_store,
        load_profile("seller"),
        chat=seller_chat.chat,
        card_url="http://testserver",
    )
    try:
        async def run() -> dict[str, Any]:
            async with loopback_client(app) as loop:
                client = A2AClient("http://testserver", http_client=loop)
                return await negotiate(
                    client,
                    load_profile("buyer"),
                    verbose=False,
                    chat=buyer_chat.chat,
                )

        outcome = asyncio.run(run())
    finally:
        seller_store.close()
        trace.reset()

    assert outcome["outcome"] == "completed", outcome
    # Seller accepts the buyer's round-6 LLM offer; that's the negotiated terms.
    assert outcome["terms"] == {"price": 750, "delivery_days": 20, "revisions": 3}

    events = read_events(trace_path)
    outbound = [e for e in events if e["direction"] == "out" and e["kind"] in {"offer", "counter", "accept"}]
    by_source = {(e["agent"], e.get("source")) for e in outbound}
    assert ("buyer", "llm") in by_source
    assert ("seller", "llm") in by_source
    assert not any(source == "fallback" for _agent, source in by_source)
    assert seller_chat.count == len(SELLER_RESPONSES)
    assert buyer_chat.count == len(BUYER_RESPONSES)
