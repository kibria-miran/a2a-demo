"""Guards for bugs promoted out of evals/reviews (E-57).

Each entry below documents a past defect that tests must keep failing on
when the fix is reverted. The standing rule: discovered bug -> runbook
section + this file, same commit as the fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.conftest import loopback_client, make_message

from src import protocol
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
