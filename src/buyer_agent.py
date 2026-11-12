"""Buyer strategy: opening offer, accept/reject, counters, resume.

Stateless: own offer history is derived from the task view, so a restart can
continue from ``tasks/get`` without hidden state.
"""

from __future__ import annotations

from typing import Any

from src import config
from src.decision import Decision
from src.negotiator import FALLBACK_RATIONALE, decide_accept, fallback_offer
from src.utility import Profile


def _role(message: dict[str, Any]) -> str:
    role = message.get("role")
    return "user" if role in {"ROLE_USER", "user"} else "agent"


def _state(view: dict[str, Any]) -> str:
    state = str(view.get("status", {}).get("state", "TASK_STATE_WORKING"))
    return config.INTERNAL_STATE_BY_A2A.get(state, state)


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
                    }
                )
    return offers


def next_move(view: dict[str, Any], profile: Profile) -> Decision:
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
        return Decision(
            kind="offer",
            terms=terms,
            rationale=FALLBACK_RATIONALE,
            round=0,
            own_u=profile.utility(terms),
            source="fallback",
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
    fallback = fallback_offer(profile, response_round, history)
    return Decision(
        kind="offer",
        terms=fallback,
        rationale=FALLBACK_RATIONALE,
        round=response_round,
        own_u=profile.utility(fallback),
        source="fallback",
    )
