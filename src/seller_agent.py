"""Seller strategy: code decides, the LLM (Phase 3) only proposes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src import config
from src.decision import Decision
from src.negotiator import (
    FALLBACK_RATIONALE,
    decide_accept,
    fallback_offer,
    validate_proposal,
)
from src.utility import Profile

Proposer = Callable[[dict[str, Any], int], dict[str, Any]]


def respond(
    task_view: dict[str, Any],
    incoming: dict[str, Any],
    profile: Profile,
    own_history: list[float] | None = None,
    propose: Proposer | None = None,
) -> Decision:
    """Answer an incoming offer (R14 turn). Accepts are handled by the server.

    Phase 1 passes ``propose=None`` and therefore always uses the fallback.
    """

    terms = incoming["terms"]
    round_number = int(incoming["round"]) + 1

    if decide_accept(terms, profile, round_number):
        return Decision(
            kind="accept",
            terms=dict(terms),
            rationale="Accepting the current offer.",
            round=round_number,
            own_u=profile.utility(terms),
            source="code",
        )

    if round_number > config.MAX_ROUNDS:
        return Decision(
            kind="walk_away",
            terms=dict(terms),
            rationale="max_rounds",
            round=round_number,
            source="code",
        )

    history = list(own_history or [])

    if propose is not None:
        proposal = propose(task_view, round_number)
        ok, reason = validate_proposal(proposal, profile, round_number, history)
        if not ok:
            # One retry, exactly as the LLM path will do (D-13).
            proposal = propose(task_view, round_number)
            ok, reason = validate_proposal(proposal, profile, round_number, history)
        if ok and proposal is not None:
            proposal_terms = {
                "price": int(proposal["terms"]["price"]),
                "delivery_days": int(proposal["terms"]["delivery_days"]),
                "revisions": int(proposal["terms"]["revisions"]),
            }
            return Decision(
                kind="counter",
                terms=proposal_terms,
                rationale=str(proposal.get("rationale", "")),
                round=round_number,
                own_u=profile.utility(proposal_terms),
                source="llm",
            )

    fallback = fallback_offer(profile, round_number, history)
    return Decision(
        kind="counter",
        terms=fallback,
        rationale=FALLBACK_RATIONALE,
        round=round_number,
        own_u=profile.utility(fallback),
        source="fallback",
    )
