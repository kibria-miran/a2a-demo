"""Seller strategy: code decides, the LLM only proposes (sections 8.6, D-5)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src import config, llm, prompts, trace
from src.decision import Decision
from src.negotiator import (
    FALLBACK_RATIONALE,
    REASON_V1_SCHEMA,
    contains_instruction,
    decide_accept,
    fallback_offer,
    validate_proposal,
)
from src.utility import Profile

Chat = Callable[..., str]
Proposer = Callable[[dict[str, Any], int], dict[str, Any] | None]


def llm_propose(
    profile: Profile,
    round_number: int,
    own_history: list[float],
    incoming: dict[str, Any],
    peer_text: str | None,
    retry_reason: str | None,
    chat: Chat,
) -> dict[str, Any] | None:
    """One LLM attempt. ``LLMError`` is propagated as a valid failure (E-50)."""

    messages = prompts.build_proposer_messages(
        profile,
        round_number,
        own_history,
        incoming["terms"],
        peer_text,
        retry_reason,
    )
    raw = chat(
        messages,
        role="proposer",
        temperature=config.LLM_TEMPERATURE,
    )
    return prompts.parse_proposal(raw)


def respond(
    task_view: dict[str, Any],
    incoming: dict[str, Any],
    profile: Profile,
    own_history: list[float] | None = None,
    propose: Proposer | None = None,
    chat: Chat | None = None,
) -> Decision:
    """Answer an incoming offer (R14 turn). Accepts are handled by the server."""

    terms = incoming["terms"]
    round_number = int(incoming["round"]) + 1
    peer_text = incoming.get("_peer_text")
    tokens_before = llm.token_usage_all()

    if peer_text and contains_instruction(peer_text):
        trace.trace(
            "counter",
            direction="in",
            agent="seller",
            task_id=(task_view or {}).get("id"),
            round=int(incoming["round"]),
            severity="error",
            error_kind="injection_attempt",
        )

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

    attempt: Callable[[str | None], dict[str, Any] | None] | None = None
    if propose is not None:
        attempt = lambda _reason: propose(task_view, round_number)  # noqa: E731
    elif chat is not None:
        attempt = lambda reason: llm_propose(  # noqa: E731
            profile,
            round_number,
            history,
            incoming,
            peer_text,
            reason,
            chat,
        )

    if attempt is not None:
        retry_reason: str | None = None
        for _ in range(config.LLM_RETRY_ATTEMPTS + 1):
            try:
                proposal = attempt(retry_reason)
            except llm.LLMError:
                proposal = None
                retry_reason = llm.REASON_LLM_ERROR
                continue
            ok, reason = validate_proposal(
                proposal, profile, round_number, history
            )
            tokens_in, tokens_out = llm.usage_delta(tokens_before)
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
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            retry_reason = reason or REASON_V1_SCHEMA

    fallback = fallback_offer(profile, round_number, history)
    tokens_in, tokens_out = llm.usage_delta(tokens_before)
    return Decision(
        kind="counter",
        terms=fallback,
        rationale=FALLBACK_RATIONALE,
        round=round_number,
        own_u=profile.utility(fallback),
        source="fallback",
        tokens_in=tokens_in if attempt is not None else None,
        tokens_out=tokens_out if attempt is not None else None,
    )
