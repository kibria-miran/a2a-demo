"""Buyer strategy: opening fallback, validated LLM counters, accept/cancel.

Stateless: own offer history is derived from the task view, so a restart can
continue from ``tasks/get`` without hidden state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src import config, llm, prompts
from src.decision import Decision
from src.negotiator import (
    FALLBACK_RATIONALE,
    REASON_V1_SCHEMA,
    decide_accept,
    fallback_offer,
    validate_proposal,
)
from src.utility import Profile

Chat = Callable[..., str]
Proposer = Callable[[dict[str, Any], int], dict[str, Any] | None]


def _role(message: dict[str, Any]) -> str:
    role = message.get("role")
    return "user" if role in {"ROLE_USER", "user"} else "agent"


def _state(view: dict[str, Any]) -> str:
    state = str(view.get("status", {}).get("state", "TASK_STATE_WORKING"))
    return config.INTERNAL_STATE_BY_A2A.get(state, state)


def _text_of(message: dict[str, Any]) -> str | None:
    for part in message.get("parts") or []:
        text = part.get("text")
        if isinstance(text, str):
            return text
    return None


def _offers(view: dict[str, Any], role: str) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    for message in view.get("history") or []:
        if _role(message) != role:
            continue
        for part in message.get("parts") or []:
            data = part.get("data")
            if (
                isinstance(data, dict)
                and data.get("type") in {"offer", "counter"}
                and isinstance(data.get("terms"), dict)
                and isinstance(data.get("round"), int)
            ):
                offers.append(
                    {
                        "round": int(data["round"]),
                        "terms": data["terms"],
                        "type": data["type"],
                        "text": _text_of(message),
                    }
                )
    return offers


def _rephrase(
    chat: Chat,
    terms: dict[str, int],
    rationale: str,
    retry_reason: str | None,
) -> str | None:
    raw = chat(
        prompts.build_rephrase_messages(0, terms, rationale, retry_reason),
        role="rephraser",
        temperature=config.LLM_TEMPERATURE,
    )
    return prompts.parse_rationale(raw)


def _llm_offer(
    chat: Chat,
    profile: Profile,
    round_number: int,
    history: list[float],
    peer_terms: dict[str, int],
    peer_text: str | None,
    retry_reason: str | None,
) -> dict[str, Any] | None:
    raw = chat(
        prompts.build_proposer_messages(
            profile,
            round_number,
            history,
            peer_terms,
            peer_text,
            retry_reason,
        ),
        role="proposer",
        temperature=config.LLM_TEMPERATURE,
    )
    return prompts.parse_proposal(raw)


def _valid_proposal(
    proposal: dict[str, Any] | None,
    profile: Profile,
    round_number: int,
    history: list[float],
) -> tuple[dict[str, int] | None, str | None]:
    ok, reason = validate_proposal(proposal, profile, round_number, history)
    if not ok or proposal is None:
        return None, reason or REASON_V1_SCHEMA
    return (
        {
            "price": int(proposal["terms"]["price"]),
            "delivery_days": int(proposal["terms"]["delivery_days"]),
            "revisions": int(proposal["terms"]["revisions"]),
        },
        None,
    )


def next_move(
    view: dict[str, Any],
    profile: Profile,
    *,
    propose: Proposer | None = None,
    chat: Chat | None = None,
) -> Decision:
    state = _state(view)
    messages = view.get("history") or []

    if state == "completed":
        return Decision(kind="complete")
    if state == "rejected":
        return Decision(kind="rejected", reason="opponent walked away")
    if state == "canceled":
        return Decision(kind="canceled")
    if state == "failed":
        return Decision(kind="failed", reason="turn failed")

    # D-17/E-16: the last message is ours and there is no agent reply.
    if messages and _role(messages[-1]) == "user" and state == "input-required":
        return Decision(kind="resend", message=messages[-1])

    buyer_offers = _offers(view, "user")
    seller_offers = _offers(view, "agent")

    if not messages:
        terms = fallback_offer(profile, 0, [])
        rationale = FALLBACK_RATIONALE
        source = "fallback"

        # E-54: round 0 terms are always the fallback; the LLM may only
        # rephrase the rationale, and the same validators still apply.
        if chat is not None:
            retry_reason: str | None = None
            for _ in range(config.LLM_RETRY_ATTEMPTS + 1):
                try:
                    candidate = _rephrase(chat, terms, rationale, retry_reason)
                except llm.LLMError:
                    candidate = None
                    retry_reason = llm.REASON_LLM_ERROR
                    continue
                if candidate is None:
                    retry_reason = REASON_V1_SCHEMA
                    continue
                ok, reason = validate_proposal(
                    {"terms": terms, "rationale": candidate}, profile, 0, []
                )
                if ok:
                    rationale = candidate
                    source = "llm"
                    break
                retry_reason = reason or REASON_V1_SCHEMA

        return Decision(
            kind="offer",
            terms=terms,
            rationale=rationale,
            round=0,
            own_u=profile.utility(terms),
            source=source,
        )

    if not seller_offers:
        return Decision(kind="wait")

    seller_offer = seller_offers[-1]
    own_last_round = buyer_offers[-1]["round"] if buyer_offers else -1
    response_round = int(seller_offer["round"]) + 1

    # E-45: the buyer validates the seller counter's round before anything else.
    if int(seller_offer["round"]) != own_last_round + 1:
        return Decision(kind="protocol_error", reason="counter round mismatch")

    terms = seller_offer["terms"]
    if decide_accept(terms, profile, response_round):
        return Decision(
            kind="accept",
            terms=dict(terms),
            rationale="Accepting the current offer.",
            round=response_round,
            own_u=profile.utility(terms),
            source="code",
        )

    if response_round > config.MAX_ROUNDS:
        return Decision(kind="cancel", reason="max_rounds")

    history = [profile.utility(offer["terms"]) for offer in buyer_offers]

    attempt: Callable[[str | None], dict[str, Any] | None] | None = None
    if propose is not None:
        attempt = lambda _reason: propose(view, response_round)  # noqa: E731
    elif chat is not None:
        attempt = lambda reason: _llm_offer(  # noqa: E731
            chat,
            profile,
            response_round,
            history,
            terms,
            seller_offer.get("text"),
            reason,
        )

    if attempt is not None:
        attempt_reason: str | None = None
        for _ in range(config.LLM_RETRY_ATTEMPTS + 1):
            try:
                proposal = attempt(attempt_reason)
            except llm.LLMError:
                proposal = None
                attempt_reason = llm.REASON_LLM_ERROR
                continue
            proposal_terms, reason = _valid_proposal(
                proposal, profile, response_round, history
            )
            if proposal_terms is not None:
                rationale = proposal.get("rationale", "") if proposal else ""
                return Decision(
                    kind="offer",
                    terms=proposal_terms,
                    rationale=str(rationale),
                    round=response_round,
                    own_u=profile.utility(proposal_terms),
                    source="llm",
                )
            attempt_reason = reason

    fallback = fallback_offer(profile, response_round, history)
    return Decision(
        kind="offer",
        terms=fallback,
        rationale=FALLBACK_RATIONALE,
        round=response_round,
        own_u=profile.utility(fallback),
        source="fallback",
    )
