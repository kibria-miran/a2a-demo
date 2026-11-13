"""Offline guards for the X2/X3/X5 experiment runner (section 13.3).

The live arms need NIM credentials, so these tests exercise the same code
paths with injected ``FakeLLM`` chats: they prove the runner wires both
sides, records tokens, and honors the rationale switch without any network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from evals.run_evals import MissingCredentials, run_one, run_x2, run_x5
from tests.conftest import FakeLLM, loopback_client

from src import config, llm
from src.buyer_cli import _message_for, negotiate
from src.client import A2AClient
from src.decision import Decision
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


def test_x2_fallback_arm_completes_offline() -> None:
    result = asyncio.run(run_one("x2", "fallback", "fallback-0"))

    assert result.outcome == "completed"
    assert result.mode == "fallback"
    assert result.terms is not None
    assert result.buyer_u is not None and result.seller_u is not None
    assert result.joint_u == pytest.approx(result.buyer_u + result.seller_u)
    assert result.tokens_in == 0 and result.tokens_out == 0


def test_x2_live_arm_wires_both_scripted_sides_and_counts_tokens() -> None:
    result = asyncio.run(
        run_one(
            "x2",
            "live",
            "llm_both-0",
            seller_chat=FakeLLM(SELLER_RESPONSES).chat,
            buyer_chat=FakeLLM(BUYER_RESPONSES).chat,
        )
    )

    assert result.outcome == "completed", result
    assert result.terms == {"price": 750, "delivery_days": 20, "revisions": 3}
    assert result.tokens_in > 0 and result.tokens_out > 0
    assert result.seed["include_rationale"] is True


def test_x2_live_without_credentials_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(config, "NIM_MODEL", "")

    with pytest.raises(MissingCredentials, match="NVIDIA_API_KEY"):
        asyncio.run(run_one("x2", "live", "t"))


def test_x2_refuses_non_live_mode() -> None:
    with pytest.raises(SystemExit, match="--mode live"):
        run_x2("fallback", 1)


def test_x5_refuses_identical_models() -> None:
    with pytest.raises(SystemExit, match="DIFFERENT"):
        run_x5("live", 1, seller_model="model-a", buyer_model="model-a")


def test_default_chat_binds_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "dummy")
    monkeypatch.setattr(config, "NIM_MODEL", "default-model")

    bound = llm.default_chat("other-model")

    assert bound is not llm.chat
    assert callable(bound)
    assert bound.keywords == {"model": "other-model"}
    assert llm.default_chat() is llm.chat


def test_x3_buyer_message_suppresses_rationale_when_disabled() -> None:
    decision = Decision(
        kind="offer",
        terms={"price": 800, "delivery_days": 14, "revisions": 3},
        rationale="A friendly sentence.",
        round=2,
    )

    on = _message_for(decision, None, "ctx")
    off = _message_for(decision, None, "ctx", include_rationale=False)

    assert any("text" in part for part in on["parts"])
    assert not any("text" in part for part in off["parts"])
    assert [part for part in off["parts"] if "data" in part] == off["parts"]


def _run_scripted_negotiation(tmp_path: Path, *, include_rationale: bool) -> Store:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    seller_chat = FakeLLM(SELLER_RESPONSES)
    buyer_chat = FakeLLM(BUYER_RESPONSES)
    app = create_app(
        store,
        load_profile("seller"),
        chat=seller_chat.chat,
        card_url="http://testserver",
        include_rationale=include_rationale,
    )

    async def run() -> dict[str, Any]:
        async with loopback_client(app) as loop:
            client = A2AClient("http://testserver", http_client=loop)
            return await negotiate(
                client,
                load_profile("buyer"),
                verbose=False,
                chat=buyer_chat.chat,
                include_rationale=include_rationale,
            )

    outcome = asyncio.run(run())
    assert outcome["outcome"] == "completed", outcome
    return store


def test_x3_server_suppresses_rationale_parts_on_the_wire(tmp_path: Path) -> None:
    off_store = _run_scripted_negotiation(tmp_path / "off", include_rationale=False)
    try:
        task_id = off_store.conn.execute("SELECT id FROM tasks").fetchone()[0]
        messages = off_store.messages_for_task(task_id)
        assert messages
        assert all(
            not any("text" in part for part in message["parts"])
            for message in messages
        ), "rationale-off run must send data parts only"
    finally:
        off_store.close()

    on_store = _run_scripted_negotiation(tmp_path / "on", include_rationale=True)
    try:
        task_id = on_store.conn.execute("SELECT id FROM tasks").fetchone()[0]
        messages = on_store.messages_for_task(task_id)
        assert any(
            any("text" in part for part in message["parts"])
            for message in messages
        ), "rationale-on run must include text parts"
    finally:
        on_store.close()
