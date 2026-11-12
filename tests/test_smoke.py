"""Phase 0 gate: conftest factories, FakeLLM, clock and loopback (E-56).

No test_server.py line may be written until this module passes (D-12).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI, Request

import conftest
from src import clock

pytestmark = pytest.mark.fast


def test_factory_semantics() -> None:
    # Two calls with no overrides produce equal, independent instances.
    a = conftest.make_offer()
    b = conftest.make_offer()
    assert a == b
    assert a is not b
    assert a["terms"] is not b["terms"]

    # Every default field is present.
    assert set(a) == {"type", "terms", "round"}
    assert set(a["terms"]) == {"price", "delivery_days", "revisions"}
    assert a["type"] == "offer"
    assert a["round"] == 0

    # Overrides apply, including nested term overrides.
    c = conftest.make_offer(
        terms={"price": 999, "delivery_days": 10, "revisions": 2}
    )
    assert c != a
    assert c["terms"] == {"price": 999, "delivery_days": 10, "revisions": 2}
    assert a["terms"]["price"] == 800  # no shared mutable state

    profile = conftest.make_profile()
    assert set(profile["issues"]) == {"price", "delivery_days", "revisions"}
    for issue in profile["issues"].values():
        assert {"direction", "ideal", "reservation", "weight"} <= set(issue)
    assert profile["beta"] == 2.0

    message = conftest.make_message()
    assert {"messageId", "role", "parts", "taskId", "contextId"} <= set(message)
    assert message["role"] == "user"
    assert len(message["parts"]) == 1

    task = conftest.make_task()
    assert {"id", "contextId", "status", "history", "artifacts"} <= set(task)
    assert task["status"]["state"] == "submitted"
    assert conftest.make_task(status={"state": "working"})["status"]["state"] == "working"


def test_fake_llm_semantics() -> None:
    llm = conftest.FakeLLM(["a", "b"])
    assert llm.peek_all() == ["a", "b"]  # peek does not consume

    assert llm.chat([{"role": "user", "content": "x"}]) == "a"
    assert llm.chat([{"role": "user", "content": "y"}]) == "b"
    assert llm.count == 2
    assert llm.calls[0]["role"] == "proposer"
    assert llm.calls[0]["temperature"] == 0.3

    llm.reset()
    assert llm.count == 0
    assert llm.chat([]) == "a"  # rewound


def test_fake_llm_exhausted_is_a_test_bug() -> None:
    llm = conftest.FakeLLM([])
    with pytest.raises(pytest.fail.Exception):
        llm.chat([])


def test_inject_clock_advances_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = conftest.inject_clock(
        monkeypatch, start=clock.now().replace(microsecond=0), step=timedelta(seconds=5)
    )

    first = clock.now()
    second = clock.now()

    assert first == fake.start
    assert second - first == timedelta(seconds=5)
    assert fake.calls == [first, second]


async def test_loopback_client_round_trip() -> None:
    app = FastAPI()

    @app.post("/rpc")
    async def rpc(request: Request) -> dict[str, object]:
        payload = await request.json()
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"ok": True}}

    async with conftest.loopback_client(app) as client:
        response = await client.post(
            "/rpc", json={"jsonrpc": "2.0", "id": 7, "method": "Ping", "params": {}}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["result"] == {"ok": True}


def test_fast_db_uses_memory_journal(db_conn) -> None:
    mode = db_conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "memory"
