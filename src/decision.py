"""Small decision object shared by the buyer and seller agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Decision:
    kind: str  # offer | counter | accept | walk_away | cancel | resend | wait | ...
    terms: dict[str, int] | None = None
    rationale: str = ""
    round: int = 0
    own_u: float | None = None
    source: str = "code"  # llm | fallback | code
    message: dict[str, Any] | None = field(default=None)
    reason: str | None = None
