"""Buyer strategy tests: opening, counters, accept, resume, cancel."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src import config
from src.buyer_agent import next_move
from src.utility import Profile

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> Profile:
    return Profile.load(json.loads((ROOT / "profiles" / f"{name}.json").read_text()))


def offer_message(
    role: str,
    type_: str,
    terms: dict[str, int],
    round_number: int,
    message_id: str,
) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "role": role,
        "parts": [
            {"data": {"type": type_, "terms": terms, "round": round_number}},
            {"text": "An offer."},
        ],
    }


def view(state: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "t1",
        "status": {"state": state},
        "history": messages,
        "artifacts": [],
    }


def test_empty_view_opens_at_round_zero() -> None:
    profile = load("buyer")
    decision = next_move(view("TASK_STATE_INPUT_REQUIRED", []), profile)
    assert decision.kind == "offer"
    assert decision.round == 0
    assert decision.source == "fallback"
    assert decision.terms is not None
    assert profile.feasible(decision.terms)
    assert abs(profile.utility(decision.terms) - config.START_U) <= config.EPS_ROUNDING


def test_waits_while_its_own_offer_is_in_flight() -> None:
    profile = load("buyer")
    own = offer_message("ROLE_USER", "offer", {"price": 680, "delivery_days": 7, "revisions": 5}, 0, "u0")
    decision = next_move(view("TASK_STATE_WORKING", [own]), profile)
    assert decision.kind == "wait"


def test_counters_after_seller_round_one_monotonically() -> None:
    profile = load("buyer")
    own_terms = {"price": 680, "delivery_days": 7, "revisions": 5}
    history = [
        offer_message("ROLE_USER", "offer", own_terms, 0, "u0"),
        offer_message(
            "ROLE_AGENT",
            "counter",
            {"price": 1130, "delivery_days": 19, "revisions": 1},
            1,
            "a1",
        ),
    ]
    decision = next_move(view("TASK_STATE_INPUT_REQUIRED", history), profile)
    assert decision.kind == "offer"
    assert decision.round == 2
    assert decision.terms is not None
    assert profile.feasible(decision.terms)
    assert profile.utility(decision.terms) <= profile.utility(own_terms) + config.EPS_MONOTONE
    assert abs(profile.utility(decision.terms) - profile.target_utility(2)) <= config.EPS_ROUNDING
    assert decision.rationale and decision.rationale[0] != "{"


def test_accepts_an_offer_that_meets_the_curve() -> None:
    profile = load("buyer")
    terms = {"price": 600, "delivery_days": 7, "revisions": 5}
    history = [
        offer_message("ROLE_USER", "offer", {"price": 680, "delivery_days": 7, "revisions": 5}, 0, "u0"),
        offer_message("ROLE_AGENT", "counter", terms, 1, "a1"),
    ]
    decision = next_move(view("TASK_STATE_INPUT_REQUIRED", history), profile)
    assert decision.kind == "accept"
    assert decision.terms == terms
    assert decision.round == 2
    assert decision.source == "code"


def test_rejects_a_counter_with_a_broken_round() -> None:
    profile = load("buyer")
    history = [
        offer_message("ROLE_USER", "offer", {"price": 680, "delivery_days": 7, "revisions": 5}, 0, "u0"),
        offer_message(
            "ROLE_AGENT",
            "counter",
            {"price": 900, "delivery_days": 14, "revisions": 3},
            2,
            "a2",
        ),
    ]
    decision = next_move(view("TASK_STATE_INPUT_REQUIRED", history), profile)
    assert decision.kind == "protocol_error"
    assert decision.reason == "counter round mismatch"


def test_resends_an_unanswered_own_message() -> None:
    profile = load("buyer")
    own = offer_message("ROLE_USER", "offer", {"price": 680, "delivery_days": 7, "revisions": 5}, 0, "u0")
    decision = next_move(view("TASK_STATE_INPUT_REQUIRED", [own]), profile)
    assert decision.kind == "resend"
    assert decision.message == own


def test_cancels_at_max_rounds_without_accepting_low_utility() -> None:
    profile = load("buyer")
    history = [
        offer_message("ROLE_USER", "offer", {"price": 900, "delivery_days": 30, "revisions": 1}, 8, "u8"),
        offer_message(
            "ROLE_AGENT",
            "counter",
            {"price": 1000, "delivery_days": 30, "revisions": 1},
            9,
            "a9",
        ),
    ]
    decision = next_move(view("TASK_STATE_INPUT_REQUIRED", history), profile)
    assert decision.kind == "cancel"
    assert decision.reason == "max_rounds"


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        ("TASK_STATE_COMPLETED", "complete"),
        ("TASK_STATE_REJECTED", "rejected"),
        ("TASK_STATE_CANCELED", "canceled"),
        ("TASK_STATE_FAILED", "failed"),
    ],
)
def test_terminal_states_map_to_decisions(state: str, kind: str) -> None:
    decision = next_move(view(state, []), load("buyer"))
    assert decision.kind == kind


def test_decisions_are_stateless_for_the_same_view() -> None:
    profile = load("buyer")
    snapshot = view("TASK_STATE_INPUT_REQUIRED", [])
    first = next_move(snapshot, profile)
    second = next_move(snapshot, profile)
    assert first == second
