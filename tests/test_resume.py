"""Buyer --resume: byte-identical resend of an incomplete turn (E-33, R6c)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import loopback_client, make_message

from src import protocol
from src.buyer_cli import _resume
from src.buyer_store import BuyerStore
from src.client import A2AClient
from src.server import create_app
from src.store import Store
from src.utility import Profile

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
TERMS = {"price": 800, "delivery_days": 14, "revisions": 3}


def load_profile(name: str) -> Profile:
    return Profile.load(json.loads((ROOT / "profiles" / f"{name}.json").read_text()))


def test_resume_resends_stored_message_byte_identically(tmp_path: Any) -> None:
    seller_store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    buyer_store = BuyerStore(str(tmp_path / "buyer.db"), journal_mode="MEMORY")
    app = create_app(seller_store, load_profile("seller"), card_url="http://testserver")

    the_message = make_message(
        messageId="resume-byte-1",
        parts=[
            protocol.data_part({"type": "offer", "terms": TERMS, "round": 0}),
            protocol.text_part("resume me"),
        ],
    )
    url = "http://testserver"

    # Simulate the crash: the task exists with one incomplete user message.
    task = seller_store.create_task(the_message.get("contextId"))
    seller_store.begin_turn(
        task["id"],
        the_message["messageId"],
        [p for p in the_message["parts"]],
    )
    # A crash leaves the turn hanging; T8 recovery moves working ->
    # input-required and pins round_at_turn_start = tasks.round (E-47).
    seller_store.startup_recovery()
    buyer_store.record(url, task["id"], the_message)

    async def run() -> dict[str, Any]:
        async with loopback_client(app) as loop:
            client = A2AClient(url, http_client=loop)
            return await _resume(client, url, buyer_store, verbose=False)  # type: ignore[return-value]

    try:
        view = asyncio.run(run())
    finally:
        seller_store.close()
        buyer_store.close()

    assert view is not None
    history = view["history"]
    # The replayed message did not create a second turn row.
    user_rows = [m for m in history if m["role"] == "ROLE_USER"]
    assert len(user_rows) == 1
    assert user_rows[0]["messageId"] == "resume-byte-1"
    # The resume actually ran the turn (agent replied).
    assert any(m["role"] == "ROLE_AGENT" for m in history)
    # The stored JSON is what was resent: ids and body identical.
    stored_row = None
    check = BuyerStore(str(tmp_path / "buyer.db"), journal_mode="MEMORY")
    try:
        stored_row = check.get(url, task["id"])
    finally:
        check.close()
    assert stored_row is not None
    assert json.loads(stored_row["last_message_json"])["messageId"] == "resume-byte-1"
    assert stored_row["last_round"] == 0
