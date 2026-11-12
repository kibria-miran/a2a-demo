"""Pure negotiation logic: lattice fallback, validators, acceptance.

No I/O. The LLM never decides; this module is the code that does.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from typing import Any

from src import config
from src.utility import ISSUES, Profile

# ---------------------------------------------------------------------------
# Stable validator reasons (E-29, E-25)
# ---------------------------------------------------------------------------

REASON_V1_SCHEMA = "V1 schema invalid"
REASON_V2_INFEASIBLE = "V2 infeasible terms"
REASON_V3_BAND = "V3 outside concession band"
REASON_V4_MONOTONE = "V4 non-monotone utility"
REASON_V5_LEAK = "V5 private value leak"
REASON_V6_INSTRUCTION = "V6 instruction phrase"
REASON_V7_MARKUP = "V7 markup token"

FALLBACK_RATIONALE = "Adjusted counter-offer within the agreed scope."

LEAK_PHRASES = (
    "my minimum",
    "my reservation",
    "my limit",
    "my bottom line",
    "lowest i can",
)

INSTRUCTION_PHRASES = (
    "ignore",
    "disregard",
    "override",
    "forget",
    "system prompt",
    "you are now",
    "new instructions",
    "you must accept",
    "reveal your",
    "as an ai",
)

MARKUP_TOKENS = ("</", "<peer", "```", "###")

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def normalize_text(text: str) -> str:
    """NFKC, strip zero-width chars, lowercase (E-14)."""

    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).lower()


def _strip_thousands(text: str) -> str:
    return re.sub(r"(?<=\d)[,\u066c\u00a0](?=\d)", "", text)


def contains_leak(text: str, profile: Profile) -> bool:
    """True if the text names an exact private value or banned phrase."""

    normalized = _strip_thousands(normalize_text(text))
    if any(phrase in normalized for phrase in LEAK_PHRASES):
        return True
    for issue in profile.issues.values():
        for private in (issue.ideal, issue.reservation):
            if re.search(rf"(?<!\d){private}(?!\d)", normalized):
                return True
    return False


def contains_instruction(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in INSTRUCTION_PHRASES)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Fallback offer: lattice search (E-1, E-54)
# ---------------------------------------------------------------------------


def _issue_values(profile: Profile, name: str) -> list[int]:
    issue = profile.issues[name]
    lo = min(issue.ideal, issue.reservation)
    hi = max(issue.ideal, issue.reservation)
    step = config.PRICE_STEP if name == "price" else 1
    values = list(range(lo, hi + 1, step))
    if values[-1] != hi:
        values.append(hi)
    if name == "price":
        # Anchor the lattice at the ideal so both ends are on the grid.
        ideal = issue.ideal
        aligned = set(values)
        aligned.add(ideal)
        values = sorted(aligned)
    return values


# Cached feasible lattice as immutable tuples: (price, days, revisions, utility).
# Profile objects contain a dict, so key on their issue values instead.
_LATTICE_CACHE: dict[
    tuple[tuple[str, str, int, int, float], ...],
    list[tuple[int, int, int, float]],
] = {}


def _profile_key(profile: Profile) -> tuple[tuple[str, str, int, int, float], ...]:
    return tuple(
        (
            name,
            profile.issues[name].direction,
            profile.issues[name].ideal,
            profile.issues[name].reservation,
            profile.issues[name].weight,
        )
        for name in ISSUES
    )


def _feasible_lattice(profile: Profile) -> list[tuple[int, int, int, float]]:
    key = _profile_key(profile)
    cached = _LATTICE_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_LATTICE_CACHE) > 64:
        _LATTICE_CACHE.clear()

    candidates: list[tuple[int, int, int, float]] = []
    for price, days, revisions in itertools.product(
        _issue_values(profile, "price"),
        _issue_values(profile, "delivery_days"),
        _issue_values(profile, "revisions"),
    ):
        terms = {"price": price, "delivery_days": days, "revisions": revisions}
        if not profile.feasible(terms):
            continue
        candidates.append((price, days, revisions, profile.utility(terms)))
    if not candidates:
        raise ValueError("profile has no feasible candidates")
    _LATTICE_CACHE[key] = candidates
    return candidates


def fallback_offer(
    profile: Profile,
    round_number: int,
    own_history: list[float] | None = None,
) -> dict[str, int]:
    """Deterministic offer closest to the concession target (E-1).

    With ``own_history == []`` the monotone filter is skipped (E-54).
    """

    history = list(own_history or [])
    target = profile.target_utility(round_number)
    feasible = _feasible_lattice(profile)

    monotone = feasible
    if history:
        ceiling = min(history) + config.EPS_MONOTONE
        monotone = [item for item in feasible if item[3] <= ceiling]
        if not monotone:
            # Nothing satisfies monotonicity: fall back to the lowest U.
            price, days, revisions, _u = min(
                feasible, key=lambda item: (item[3], item[0])
            )
            return {
                "price": price,
                "delivery_days": days,
                "revisions": revisions,
            }

    price, days, revisions, _u = min(
        monotone,
        key=lambda item: (
            abs(item[3] - target),
            abs(item[0] - profile.issues["price"].ideal),
            item[0],
            item[1],
            item[2],
        ),
    )
    return {"price": price, "delivery_days": days, "revisions": revisions}


# ---------------------------------------------------------------------------
# Validators V1..V7 (first failure wins)
# ---------------------------------------------------------------------------


def _schema_ok(proposal: Any) -> bool:
    if not isinstance(proposal, dict):
        return False
    terms = proposal.get("terms")
    rationale = proposal.get("rationale", "")
    if not isinstance(terms, dict):
        return False
    for name in ISSUES:
        if name not in terms or not _is_int(terms[name]):
            return False
    if not isinstance(rationale, str):
        return False
    return len(rationale) <= config.MAX_RATIONALE_CHARS


def validate_proposal(
    proposal: Any,
    profile: Profile,
    round_number: int,
    own_history: list[float] | None = None,
) -> tuple[bool, str | None]:
    if not _schema_ok(proposal):
        return False, REASON_V1_SCHEMA

    terms: dict[str, int] = {
        name: int(proposal["terms"][name])
        for name in ISSUES
    }
    rationale = str(proposal.get("rationale", ""))

    if not profile.feasible(terms):
        return False, REASON_V2_INFEASIBLE

    target = profile.target_utility(round_number)
    utility = profile.utility(terms)
    if not (
        target - config.BAND_LOW - config.EPS_BAND
        <= utility
        <= target + config.BAND_HIGH + config.EPS_BAND
    ):
        return False, REASON_V3_BAND

    history = list(own_history or [])
    if history and utility > min(history) + config.EPS_MONOTONE:
        return False, REASON_V4_MONOTONE

    if contains_leak(rationale, profile):
        return False, REASON_V5_LEAK

    if contains_instruction(rationale):
        return False, REASON_V6_INSTRUCTION

    if any(token in rationale for token in MARKUP_TOKENS):
        return False, REASON_V7_MARKUP

    return True, None


# ---------------------------------------------------------------------------
# Code decides acceptance (D-5, E-12)
# ---------------------------------------------------------------------------


def decide_accept(peer_terms: dict[str, Any], profile: Profile, round_number: int) -> bool:
    try:
        if not profile.feasible(peer_terms):
            return False
        utility = profile.utility(peer_terms)
    except (ValueError, TypeError):
        return False
    return utility >= profile.target_utility(round_number) - config.EPS_ROUNDING


def target_utility(profile: Profile, round_number: int) -> float:
    return profile.target_utility(round_number)


__all__ = [
    "FALLBACK_RATIONALE",
    "INSTRUCTION_PHRASES",
    "LEAK_PHRASES",
    "MARKUP_TOKENS",
    "REASON_V1_SCHEMA",
    "REASON_V2_INFEASIBLE",
    "REASON_V3_BAND",
    "REASON_V4_MONOTONE",
    "REASON_V5_LEAK",
    "REASON_V6_INSTRUCTION",
    "REASON_V7_MARKUP",
    "contains_instruction",
    "contains_leak",
    "decide_accept",
    "fallback_offer",
    "normalize_text",
    "target_utility",
    "validate_proposal",
]
