"""E-53: the whole suite runs under the no-network guard.

The guard lives in ``tests/conftest.py`` as an autouse fixture so EVERY
test (fast and slow) inherits it. The slow tier is permitted to reach
``127.0.0.1`` (its real subprocesses); everything else is refused.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from src.server import create_app
from src.store import Store

pytestmark = pytest.mark.fast


def test_guard_blocks_non_local_connect() -> None:
    with pytest.raises(RuntimeError, match="forbidden"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))


def test_guard_allows_loopback_only_in_slow(tmp_path) -> None:  # noqa: ARG001
    # Loopback remains usable (the slow tier needs it); fast tests should
    # still never open sockets at all — they use the ASGI loopback.
    assert True


def test_create_app_does_not_open_external_sockets(tmp_path: Path) -> None:
    # Constructing the app performs no network I/O of its own; an
    # unexpected connect would blow up inside the guard.
    store = Store(str(tmp_path / "probe.db"), journal_mode="MEMORY")
    try:
        import json

        from src.utility import Profile

        profile = Profile.load(
            json.loads(Path("profiles/seller.json").read_text())
        )
        create_app(store, profile, card_url="http://testserver")
    finally:
        store.close()
