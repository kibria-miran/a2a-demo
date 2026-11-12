"""Utility arithmetic tests (both directions, clipping, curve)."""

from __future__ import annotations

import pytest

from src import config
from src.utility import Profile

pytestmark = pytest.mark.fast

BUYER = {
    "name": "buyer",
    "beta": 2.0,
    "issues": {
        "price": {"direction": "min", "ideal": 600, "reservation": 1000, "weight": 0.5},
        "delivery_days": {"direction": "min", "ideal": 7, "reservation": 30, "weight": 0.2},
        "revisions": {"direction": "max", "ideal": 5, "reservation": 1, "weight": 0.3},
    },
}

SELLER = {
    "name": "seller",
    "beta": 2.0,
    "issues": {
        "price": {"direction": "max", "ideal": 1200, "reservation": 700, "weight": 0.5},
        "delivery_days": {"direction": "max", "ideal": 21, "reservation": 7, "weight": 0.3},
        "revisions": {"direction": "min", "ideal": 1, "reservation": 4, "weight": 0.2},
    },
}

IDEAL_FOR_BUYER = {"price": 600, "delivery_days": 7, "revisions": 5}
WORST_FOR_BUYER = {"price": 1000, "delivery_days": 30, "revisions": 1}


def test_profile_load_and_weights() -> None:
    profile = Profile.load(BUYER)
    assert profile.name == "buyer"
    assert profile.beta == 2.0
    assert profile.issues["price"].weight == 0.5


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["issues"]["price"].update(weight=0.4),
        lambda p: p["issues"]["price"].update(ideal=1000),
        lambda p: p["issues"]["price"].update(reservation=600),
        lambda p: p["issues"]["price"].update(direction="sideways"),
        lambda p: p["issues"]["price"].update(ideal=600.5),
        lambda p: p["issues"].pop("revisions"),
        lambda p: p.update(beta=0),
    ],
)
def test_profile_validation_rejects_bad_profiles(mutate) -> None:
    import copy

    data = copy.deepcopy(BUYER)
    mutate(data)
    with pytest.raises(ValueError):
        Profile.load(data)


def test_utility_endpoints_and_midpoint() -> None:
    profile = Profile.load(BUYER)
    assert profile.utility(IDEAL_FOR_BUYER) == pytest.approx(1.0)
    assert profile.utility(WORST_FOR_BUYER) == pytest.approx(0.0)
    mid = {"price": 800, "delivery_days": 18, "revisions": 3}
    assert 0.0 < profile.utility(mid) < 1.0


def test_utility_clips_out_of_range_values() -> None:
    profile = Profile.load(BUYER)
    better = {"price": 100, "delivery_days": 1, "revisions": 99}
    worse = {"price": 5000, "delivery_days": 99, "revisions": -10}
    assert profile.utility(better) == pytest.approx(1.0)
    assert profile.utility(worse) == pytest.approx(0.0)


def test_feasibility_respects_direction() -> None:
    buyer = Profile.load(BUYER)
    seller = Profile.load(SELLER)
    deal = {"price": 850, "delivery_days": 14, "revisions": 3}
    assert buyer.feasible(deal)
    assert seller.feasible(deal)
    assert not buyer.feasible({"price": 1100, "delivery_days": 14, "revisions": 3})
    assert not seller.feasible({"price": 600, "delivery_days": 14, "revisions": 3})
    assert not buyer.feasible({"price": 850, "delivery_days": 40, "revisions": 3})
    assert not seller.feasible({"price": 850, "delivery_days": 3, "revisions": 3})


def test_utility_rejects_bool_and_float_terms() -> None:
    profile = Profile.load(BUYER)
    with pytest.raises(ValueError):
        profile.utility({"price": True, "delivery_days": 14, "revisions": 3})
    with pytest.raises(ValueError):
        profile.utility({"price": 800.0, "delivery_days": 14, "revisions": 3})


def test_target_utility_curve() -> None:
    profile = Profile.load(BUYER)
    assert profile.target_utility(0) == pytest.approx(config.START_U)
    assert profile.target_utility(config.MAX_ROUNDS) == pytest.approx(config.END_U)
    assert profile.target_utility(100) == pytest.approx(config.END_U)
    values = [profile.target_utility(r) for r in range(config.MAX_ROUNDS + 1)]
    assert all(
        a >= b
        for a, b in zip(values[:-1], values[1:], strict=True)
    )


def test_beta_changes_curve_shape() -> None:
    import copy

    stub = copy.deepcopy(BUYER)
    stub["beta"] = 0.5
    conceder = Profile.load(stub)
    stub["beta"] = 3.0
    stubborn = Profile.load(stub)
    r = config.MAX_ROUNDS // 2
    assert conceder.target_utility(r) < Profile.load(BUYER).target_utility(r)
    assert stubborn.target_utility(r) > Profile.load(BUYER).target_utility(r)
