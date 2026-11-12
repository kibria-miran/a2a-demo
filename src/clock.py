"""Single source of timestamps (D-11).

Nothing in this project calls ``datetime.now()`` directly. Tests monkeypatch
``clock.now`` through ``tests/conftest.py::inject_clock``.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""

    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""

    if dt.tzinfo is None:
        raise ValueError("clock.to_iso requires a timezone-aware datetime")
    return dt.astimezone(UTC).isoformat()
