"""Trace module: quantization, field hygiene, end-to-end emission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import loopback_client

from src import trace
from src.buyer_cli import negotiate
from src.client import A2AClient
from src.server import create_app
from src.store import Store
from src.utility import Profile

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
TERMS = {"price": 800, "delivery_days": 14, "revisions": 3}


def load_profile(name: str) -> Profile:
    return Profile.load(json.loads((ROOT / "profiles" / f"{name}.json").read_text()))


@pytest.fixture
def sink_dir(tmp_path: Any):
    path = tmp_path / "trace.jsonl"
    trace.configure(path)
    try:
        yield path
    finally:
        trace.reset()


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def test_disabled_by_default(tmp_path: Any) -> None:
    path = tmp_path / "unused.jsonl"
    trace.reset()
    trace.trace("offer", direction="out", agent="buyer", task_id="t", round=0)
    assert not path.exists()


def test_quantizes_own_u_and_target_u(sink_dir: Path) -> None:
    trace.trace(
        "counter",
        direction="out",
        agent="seller",
        task_id="t1",
        round=1,
        terms=TERMS,
        own_u=0.62789,
        target_u=0.71233,
        source="fallback",
    )
    (event,) = read_events(sink_dir)
    assert event["own_U"] == 0.63
    assert event["target_U"] == 0.71
    assert "own_history" not in event
    assert event["ts"]


def test_error_kind_only_with_severity_error(sink_dir: Path) -> None:
    trace.trace("offer", direction="in", agent="seller", round=0)
    trace.trace(
        "turn_failed",
        direction="out",
        agent="seller",
        round=1,
        severity="error",
        error_kind="turn_failed",
    )
    events = read_events(sink_dir)
    assert "error_kind" not in events[0]
    assert events[1]["severity"] == "error"
    assert events[1]["error_kind"] == "turn_failed"


def test_rejects_bad_direction_and_kind(sink_dir: Path) -> None:
    with pytest.raises(ValueError):
        trace.trace("offer", direction="sideways", agent="seller")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        trace.trace("bogus", direction="in", agent="seller")  # type: ignore[arg-type]


def test_full_negotiation_emits_quantized_trace(tmp_path: Any) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace.configure(trace_path)
    try:
        store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
        app = create_app(store, load_profile("seller"), card_url="http://testserver")
        try:
            import asyncio

            async def run() -> Any:
                async with loopback_client(app) as loop:
                    client = A2AClient("http://testserver", http_client=loop)
                    return await negotiate(client, load_profile("buyer"), verbose=False)

            result = asyncio.run(run())
        finally:
            store.close()
    finally:
        trace.reset()

    assert result["outcome"] == "completed"
    events = read_events(trace_path)
    assert events, "trace must record events for a full negotiation"
    by_kind = {event["kind"] for event in events}
    assert {"offer", "counter"} <= by_kind
    for event in events:
        assert set(event) >= {"ts", "agent", "direction", "kind", "severity"}
        assert event["direction"] in {"in", "out"}
        assert event["severity"] in {"info", "error"}
        assert "own_history" not in event
        for key in ("own_U", "target_U"):
            if key in event:
                assert round(event[key], 2) == event[key]
    agents = {event["agent"] for event in events}
    assert {"buyer", "seller"} <= agents
