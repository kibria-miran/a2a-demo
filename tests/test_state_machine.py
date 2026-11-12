"""One test per transition row T1-T8 (section 8.4)."""

from __future__ import annotations

import pytest

from src import state_machine
from src.state_machine import StateError

pytestmark = pytest.mark.fast


@pytest.mark.parametrize(
    ("src", "dst", "via_recovery"),
    [
        # T1
        ("submitted", "working", False),
        # T2
        ("submitted", "rejected", False),
        ("submitted", "canceled", False),
        ("submitted", "failed", False),
        # T3
        ("working", "input-required", False),
        # T4
        ("working", "completed", False),
        # T5
        ("working", "rejected", False),
        ("working", "canceled", False),
        ("working", "failed", False),
        # T6
        ("input-required", "working", False),
        # T7
        ("input-required", "canceled", False),
        ("input-required", "failed", False),
        # T8
        ("submitted", "input-required", True),
        ("working", "input-required", True),
    ],
)
def test_transition_rows(src: str, dst: str, via_recovery: bool) -> None:
    assert state_machine.transition(src, dst, via_recovery=via_recovery) == dst


def test_t8_requires_recovery_flag_for_submitted() -> None:
    with pytest.raises(StateError):
        state_machine.transition("submitted", "input-required")


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        ("submitted", "completed"),
        ("submitted", "input-required"),
        ("working", "submitted"),
        ("input-required", "submitted"),
        ("input-required", "completed"),
        ("input-required", "rejected"),
    ],
)
def test_illegal_non_terminal_transitions(src: str, dst: str) -> None:
    with pytest.raises(StateError):
        state_machine.transition(src, dst)


@pytest.mark.parametrize("terminal", sorted({"completed", "canceled", "failed", "rejected"}))
@pytest.mark.parametrize("dst", ["submitted", "working", "input-required", "completed"])
def test_terminal_states_are_immutable(terminal: str, dst: str) -> None:
    assert state_machine.is_terminal(terminal)
    with pytest.raises(StateError):
        state_machine.transition(terminal, dst)


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(StateError):
        state_machine.transition("bogus", "working")
    assert not state_machine.can_transition("working", "bogus")
