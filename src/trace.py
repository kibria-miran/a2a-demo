"""Append-only JSONL trace (section 8.9, E-35, E-51).

One line per event. Fields: ``ts``, ``agent``, ``task_id`` (omitted when
unknown), ``round``, ``direction`` (``in`` | ``out``), ``kind``,
``error_kind`` (only when ``severity == "error"``), ``severity``
(``info`` | ``error``), ``terms``, ``own_U``, ``target_U``, ``source``
(``llm`` | ``fallback`` | ``code``), ``tokens_in``, ``tokens_out``,
``latency_ms``.

Privacy (E-35, S5): ``own_U`` and ``target_U`` are quantized to
``config.TRACE_U_DECIMALS`` before they are written, and only the values
for the current message are traced. The private profile, ``own_history``
and the full concession curve are NEVER written.

Tracing is disabled by default (``trace()`` is a no-op) so fast tests stay
hermetic; entry points call ``configure()`` with ``config.TRACE_PATH``.
Tests point ``configure()`` at a ``tmp_path`` file and call ``reset()``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from src import clock, config

_ROUND = round  # builtin kept under an alias; `round` is a parameter name below

MESSAGE_KINDS = frozenset({"offer", "counter", "accept", "walk_away"})
LIFECYCLE_KINDS = frozenset({"replay", "resume", "discarded", "turn_failed", "server_busy"})
KINDS = MESSAGE_KINDS | LIFECYCLE_KINDS

ERROR_KINDS = frozenset(
    {
        "bad_params",
        "bad_round",
        "turn_failed",
        "injection_attempt",
        "server_busy",
        "error",
    }
)

_PATH: str | None = None


def configure(path: str | os.PathLike[str] | None) -> None:
    """Enable (or, with ``None``, disable) the trace sink."""

    global _PATH
    _PATH = os.fspath(path) if path is not None else None
    if _PATH:
        parent = os.path.dirname(_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)


def reset() -> None:
    """Disable the sink and drop the path (tests)."""

    global _PATH
    _PATH = None


def enabled() -> bool:
    return _PATH is not None


def _quantize(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), config.TRACE_U_DECIMALS)


def trace(
    kind: str,
    *,
    direction: str,
    agent: str,
    task_id: str | None = None,
    round: int | None = None,  # noqa: A002 - wire field name
    severity: str = "info",
    error_kind: str | None = None,
    terms: dict[str, Any] | None = None,
    own_u: float | None = None,
    target_u: float | None = None,
    source: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    latency_ms: float | None = None,
) -> None:
    if _PATH is None:
        return
    if kind not in KINDS:
        raise ValueError(f"unknown trace kind: {kind!r}")
    if direction not in {"in", "out"}:
        raise ValueError(f"unknown trace direction: {direction!r}")
    if severity not in {"info", "error"}:
        raise ValueError(f"unknown trace severity: {severity!r}")

    event: dict[str, Any] = {
        "ts": clock.now().isoformat(),
        "agent": agent,
        "round": round,
        "direction": direction,
        "kind": kind,
        "severity": severity,
    }
    if task_id is not None:
        event["task_id"] = task_id
    if severity == "error":
        if error_kind is None:
            error_kind = "error"
        if error_kind not in ERROR_KINDS:
            raise ValueError(f"unknown error_kind: {error_kind!r}")
        event["error_kind"] = error_kind
    if terms is not None:
        event["terms"] = terms
    q_own = _quantize(own_u)
    q_target = _quantize(target_u)
    if q_own is not None:
        event["own_U"] = q_own
    if q_target is not None:
        event["target_U"] = q_target
    if source is not None:
        event["source"] = source
    if tokens_in is not None:
        event["tokens_in"] = int(tokens_in)
    if tokens_out is not None:
        event["tokens_out"] = int(tokens_out)
    if latency_ms is not None:
        event["latency_ms"] = _ROUND(float(latency_ms), 1)

    with open(_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, separators=(",", ":")) + "\n")
