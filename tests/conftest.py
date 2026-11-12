"""Shared test fixtures and factories (E-21, E-31, E-43).

IMPORTANT for proposer-failure tests: the agent retries once (D-13), so a
test that wants the fallback offer must stage TWO bad LLM responses. One bad
response is consumed by the first attempt; the second attempt gets the
fallback only after the retry also fails.
"""

from __future__ import annotations

import copy
import socket
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from src import clock, config


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """E-53: no test may open a socket to a non-local host.

    Every test (fast and slow) runs under this guard; the slow tier is
    allowed to reach ``127.0.0.1`` for its real subprocesses. Any attempt
    to reach a real external host (e.g. NIM) fails loudly.
    """

    real_connect = socket.socket.connect

    def guard(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            raise RuntimeError(
                f"network access to {host!r} is forbidden (fast tier must be offline)"
            )
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)

# ---------------------------------------------------------------------------
# Factories. Every default field is present; overrides are deep-merged.
# ---------------------------------------------------------------------------


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_DEFAULT_ISSUES = {
    "price": {"direction": "min", "ideal": 600, "reservation": 1000, "weight": 0.5},
    "delivery_days": {
        "direction": "min",
        "ideal": 7,
        "reservation": 30,
        "weight": 0.2,
    },
    "revisions": {"direction": "max", "ideal": 5, "reservation": 1, "weight": 0.3},
}


def make_profile(**overrides: Any) -> dict[str, Any]:
    """Return a complete profile dict, buyer-shaped by default."""

    base: dict[str, Any] = {
        "name": "buyer",
        "issues": copy.deepcopy(_DEFAULT_ISSUES),
        "beta": config.BETA_DEFAULT,
    }
    return _merge(base, overrides)


_DEFAULT_TERMS = {"price": 800, "delivery_days": 14, "revisions": 3}


def make_offer(**overrides: Any) -> dict[str, Any]:
    """Return a complete offer data object (the `data` payload)."""

    base: dict[str, Any] = {
        "type": "offer",
        "terms": copy.deepcopy(_DEFAULT_TERMS),
        "round": 0,
    }
    return _merge(base, overrides)


def make_message(**overrides: Any) -> dict[str, Any]:
    """Return a complete A2A 1.0 Message dict."""

    base: dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"data": make_offer()}],
        "taskId": None,
        "contextId": None,
    }
    return _merge(base, overrides)


def make_task(**overrides: Any) -> dict[str, Any]:
    """Return a complete A2A 1.0 Task dict."""

    base: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "contextId": str(uuid.uuid4()),
        "status": {
            "state": "submitted",
            "message": None,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
        "history": [],
        "artifacts": [],
    }
    return _merge(base, overrides)


# ---------------------------------------------------------------------------
# FakeLLM
# ---------------------------------------------------------------------------


class FakeLLM:
    """Scripted stand-in for ``src.llm.chat`` (E-21)."""

    def __init__(self, chat_responses: list[str]) -> None:
        self._responses = list(chat_responses)
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        role: str = "proposer",
        temperature: float = config.LLM_TEMPERATURE,
    ) -> str:
        self.calls.append(
            {
                "role": role,
                "messages": copy.deepcopy(messages),
                "temperature": temperature,
            }
        )
        if self._index >= len(self._responses):
            pytest.fail(
                "FakeLLM exhausted: the test staged fewer responses than the "
                "agent requested (a proposer-failure test needs TWO bad "
                "responses because the agent retries once, D-13)"
            )
        response = self._responses[self._index]
        self._index += 1
        return response

    @property
    def count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        """Rewind the response index and clear recorded calls."""

        self._index = 0
        self.calls.clear()

    def peek_all(self) -> list[str]:
        """Return the full response list without consuming anything."""

        return list(self._responses)


# ---------------------------------------------------------------------------
# Clock injection
# ---------------------------------------------------------------------------


def inject_clock(
    monkeypatch: pytest.MonkeyPatch,
    start: datetime | None = None,
    step: timedelta | None = None,
) -> Any:
    """Monkeypatch ``clock.now()`` with a deterministic sequence.

    With ``step`` set, successive calls advance by that amount. The returned
    callable exposes ``.calls`` and ``.start``.
    """

    t0 = start or datetime(2026, 1, 1, tzinfo=UTC)
    delta = step or timedelta(seconds=1)
    calls: list[datetime] = []

    def fake_now() -> datetime:
        value = t0 + delta * len(calls)
        calls.append(value)
        return value

    fake_now.calls = calls  # type: ignore[attr-defined]
    fake_now.start = t0  # type: ignore[attr-defined]
    monkeypatch.setattr(clock, "now", fake_now)
    return fake_now


# ---------------------------------------------------------------------------
# HTTP loopback (no ports, no sleeps, no network)
# ---------------------------------------------------------------------------


def loopback_client(app: Any) -> httpx.AsyncClient:
    """Return an AsyncClient bound to an ASGI app in-process."""

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Fast-tier database
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path: Any) -> Iterator[sqlite3.Connection]:
    """A per-test SQLite connection with fast-tier pragmas (E-43)."""

    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path: Any) -> Any:
    return tmp_path / "test.db"
