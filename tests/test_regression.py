"""Guards for bugs promoted out of evals/reviews (E-57).

Each entry below documents a past defect that tests must keep failing on
when the fix is reverted. The standing rule: discovered bug -> runbook
section + this file, same commit as the fix.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.conftest import loopback_client, make_message

from src import config, protocol
from src.decision import Decision
from src.server import create_app
from src.store import Store
from src.utility import Profile

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]


def load_profile(name: str) -> Profile:
    return Profile.load(json.loads((ROOT / "profiles" / f"{name}.json").read_text()))


def test_peer_text_reaches_responder_on_normal_turn(tmp_path: Any) -> None:
    """A normal (non-resume) turn must hand the peer's text to the agent.

    Regression for: server stripped the message down to its data part
    before spawning the turn, so the peer's rationale never reached the
    proposer prompt (the resume path attached `_peer_text` correctly).
    """

    captured: dict[str, Any] = {}

    def capturing_responder(
        view: dict[str, Any],
        incoming: dict[str, Any],
        profile: Profile,
        own_history: list[float],
    ) -> Decision:
        captured.update(incoming)
        return Decision(
            kind="counter",
            terms={"price": 900, "delivery_days": 14, "revisions": 3},
            rationale="ok",
            round=1,
            own_u=0.5,
            source="code",
        )

    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(
        store,
        load_profile("seller"),
        responder=capturing_responder,
        card_url="http://testserver",
    )
    try:
        message = make_message(
            messageId="peer-text-1",
            parts=[
                protocol.data_part(
                    {
                        "type": "offer",
                        "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                        "round": 0,
                    }
                ),
                protocol.text_part("my budget is flexible-ish"),
            ],
        )
        env = SimpleNamespace(app=app)
        import asyncio

        async def run() -> dict[str, Any]:
            async with loopback_client(env.app) as client:
                response = await client.post(
                    "/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "SendMessage",
                        "params": {"message": message},
                    },
                )
                assert response.status_code == 200
                return response.json()

        body = asyncio.run(run())
        assert "error" not in body
    finally:
        store.close()

    assert captured.get("_peer_text") == "my budget is flexible-ish"


async def _send(http: Any, body: dict[str, Any]) -> dict[str, Any]:
    response = await http.post(config.A2A_RPC_PATH, json=body)
    assert response.status_code == 200
    return response.json()


def test_replay_of_accept_returns_the_completed_task(tmp_path: Any) -> None:
    """Replaying an accept's messageId must return the task (R6b),
    not a terminal-state error.

    Regression for: the buyer's accept message was never stored, so the
    replay path saw no stored message and fell through to R7 (-32602
    "task is terminal").
    """

    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(store, load_profile("seller"), card_url="http://testserver")
    try:
        async def run() -> None:
            async with loopback_client(app) as http:
                # Open the task; seller counters by code fallback.
                offer = make_message(
                    messageId="accept-replay-1",
                    parts=[
                        protocol.data_part(
                            {
                                "type": "offer",
                                "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                                "round": 0,
                            }
                        )
                    ],
                )
                first = await _send(
                    http, {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": offer}}
                )
                assert "error" not in first
                task = first["result"]["task"]
                history = task["history"]
                counter = next(
                    m for m in history if m["role"] == "ROLE_AGENT"
                )
                counter_data = next(
                    part["data"] for part in counter["parts"] if "data" in part
                )
                accept = make_message(
                    messageId="accept-replay-accept-1",
                    taskId=task["id"],
                    parts=[
                        protocol.data_part(
                            {
                                "type": "accept",
                                "terms": counter_data["terms"],
                                "round": counter_data["round"] + 1,
                            }
                        )
                    ],
                )
                accepted = await _send(
                    http, {"jsonrpc": "2.0", "id": 2, "method": "SendMessage", "params": {"message": accept}}
                )
                assert "error" not in accepted
                assert accepted["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

                replay = await _send(
                    http, {"jsonrpc": "2.0", "id": 3, "method": "SendMessage", "params": {"message": accept}}
                )
                assert "error" not in replay
                assert replay["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

        import asyncio

        asyncio.run(run())
    finally:
        store.close()


def test_sync_responder_does_not_block_event_loop(tmp_path: Any) -> None:
    """A sync (blocking) responder must run in a worker thread (E-4).

    Regression for: llm.chat() is synchronous; called directly from the
    async turn it monopolized the event loop, starving tasks/get and
    delaying the BLOCKING_TIMEOUT snapshot.
    """

    def blocking_responder(
        view: dict[str, Any],
        incoming: dict[str, Any],
        profile: Profile,
        own_history: list[float],
    ) -> Decision:
        time.sleep(1.0)
        return Decision(
            kind="counter",
            terms={"price": 1150, "delivery_days": 19, "revisions": 1},
            rationale="slow",
            round=1,
            own_u=0.9,
            source="llm",
        )

    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(
        store, load_profile("seller"), responder=blocking_responder, card_url="http://testserver"
    )
    try:
        async def run() -> None:
            async with loopback_client(app) as http:
                message = make_message(
                    messageId="blocking-responder-1",
                    parts=[
                        protocol.data_part(
                            {
                                "type": "offer",
                                "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                                "round": 0,
                            }
                        )
                    ],
                )
                send = asyncio.create_task(
                    http.post(
                        config.A2A_RPC_PATH,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "SendMessage",
                            "params": {"message": message},
                        },
                    )
                )
                await asyncio.sleep(0.1)  # let the handler start
                started = time.monotonic()
                card = await http.get(config.AGENT_CARD_PATH)
                elapsed = time.monotonic() - started
                assert card.status_code == 200
                assert elapsed < 0.5, "card GET must not wait for the turn"
                await send

        import asyncio

        asyncio.run(run())
    finally:
        store.close()
