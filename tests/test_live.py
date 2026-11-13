"""One real NIM negotiation over the real network — the ``live`` marker.

Skipped unless ``NVIDIA_API_KEY`` and ``NIM_MODEL`` are configured (.env).
This is the only test allowed to reach an external host: the network guard
in ``conftest.py`` exempts ``live``-marked tests. Never runs in CI.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import loopback_client

from src import config, llm, trace
from src.buyer_cli import negotiate
from src.client import A2AClient
from src.server import create_app
from src.store import Store
from src.utility import Profile

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[1]


def _load_profile(name: str) -> Profile:
    return Profile.load(json.loads((ROOT / "profiles" / f"{name}.json").read_text()))


def test_single_real_negotiation(tmp_path: Path) -> None:
    if not (config.NVIDIA_API_KEY and config.NIM_MODEL):
        pytest.skip("live: set NVIDIA_API_KEY and NIM_MODEL in .env")

    trace_path = tmp_path / "trace.jsonl"
    trace.configure(trace_path)
    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(
        store,
        _load_profile("seller"),
        chat=llm.default_chat(),
        card_url="http://testserver",
    )

    async def run() -> dict[str, Any]:
        async with loopback_client(app) as loop:
            client = A2AClient("http://testserver", http_client=loop)
            return await negotiate(
                client,
                _load_profile("buyer"),
                verbose=False,
                chat=llm.default_chat(),
            )

    try:
        outcome = asyncio.run(run())
    finally:
        store.close()
        trace.reset()

    assert outcome["outcome"] in {"completed", "rejected", "canceled"}
    if outcome["outcome"] == "completed":
        assert outcome["terms"]["price"] > 0
    assert trace_path.exists()
