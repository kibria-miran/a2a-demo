"""Store tests: schema, message invariant, CAS round bookkeeping (E-46/E-47)."""

from __future__ import annotations

import sqlite3

import pytest

from src import config, protocol
from src.store import Store, StoreError

pytestmark = pytest.mark.fast

TERMS = {"price": 800, "delivery_days": 14, "revisions": 3}


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    try:
        yield s
    finally:
        s.close()


def user_offer(round_number: int = 0) -> list[dict]:
    return [
        protocol.data_part(
            {"type": "offer", "terms": dict(TERMS), "round": round_number}
        )
    ]


def agent_counter(round_number: int = 1) -> list[dict]:
    return [
        protocol.data_part(
            {"type": "counter", "terms": dict(TERMS), "round": round_number}
        ),
        protocol.text_part("A counter."),
    ]


def test_schema_and_open_count(store: Store) -> None:
    tables = {
        row[0]
        for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"tasks", "messages", "artifacts"} <= tables
    assert store.open_task_count() == 0

    task = store.create_task()
    assert task["state"] == "submitted"
    assert task["round"] == 0
    assert task["round_at_turn_start"] is None
    assert store.open_task_count() == 1


def test_message_ids_are_globally_unique_for_users(store: Store) -> None:
    t1 = store.create_task()
    t2 = store.create_task()
    store.append_message(t1["id"], "m1", "user", user_offer())
    with pytest.raises(sqlite3.IntegrityError):
        store.append_message(t2["id"], "m1", "user", user_offer())


def test_store_rejects_non_offer_agent_message(store: Store) -> None:
    task = store.create_task()
    with pytest.raises(StoreError):
        store.append_message(
            task["id"],
            "a1",
            "agent",
            [protocol.data_part({"type": "offer", "terms": TERMS, "round": 1})],
        )
    with pytest.raises(StoreError):
        store.append_message(task["id"], "a2", "agent", [protocol.text_part("hello")])
    # A real counter is accepted.
    store.append_message(task["id"], "a3", "agent", agent_counter())


def test_begin_turn_sets_round_at_turn_start_without_advancing_round(
    store: Store,
) -> None:
    task = store.create_task()
    updated = store.begin_turn(task["id"], "u1", user_offer(0))
    assert updated["state"] == "working"
    assert updated["round"] == 0
    assert updated["round_at_turn_start"] == 0
    messages = store.messages_for_task(task["id"])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert store.message_round(messages[0]) == 0


def test_cas_counter_advances_round_by_exactly_two(store: Store) -> None:
    task = store.create_task()
    store.begin_turn(task["id"], "u1", user_offer(0))
    store.append_message(task["id"], "a1", "agent", agent_counter(1))
    assert store.cas_commit(task["id"], "input-required", "counter", own_u=0.8)
    row = store.get_task(task["id"])
    assert row is not None
    assert row["round"] == 2
    assert row["round_at_turn_start"] is None
    assert store.own_utility_history(task["id"]) == [0.8]

    # 2 -> 4 on the next counter.
    store.begin_turn(task["id"], "u2", user_offer(2))
    store.append_message(task["id"], "a2", "agent", agent_counter(3))
    assert store.cas_commit(task["id"], "input-required", "counter", own_u=0.7)
    assert store.get_task(task["id"])["round"] == 4
    assert store.own_utility_history(task["id"]) == [0.8, 0.7]


@pytest.mark.parametrize(
    ("kind", "final"),
    [("accept", "completed"), ("walk_away", "rejected")],
)
def test_cas_accept_and_walk_away_do_not_advance_round(
    store: Store, kind: str, final: str
) -> None:
    task = store.create_task()
    store.begin_turn(task["id"], "u1", user_offer(0))
    assert store.cas_commit(task["id"], final, kind)
    row = store.get_task(task["id"])
    assert row is not None
    assert row["state"] == final
    assert row["round"] == 0
    assert row["round_at_turn_start"] is None


def test_cas_discards_result_after_cancel(store: Store) -> None:
    task = store.create_task()
    store.begin_turn(task["id"], "u1", user_offer(0))
    store.cancel_task(task["id"])
    assert not store.cas_commit(task["id"], "input-required", "counter", own_u=0.8)
    assert store.get_task(task["id"])["state"] == "canceled"
    assert store.get_task(task["id"])["round"] == 0


def test_round_at_turn_start_is_null_when_idle(store: Store) -> None:
    task = store.create_task()
    store.begin_turn(task["id"], "u1", user_offer(0))
    store.append_message(task["id"], "a1", "agent", agent_counter(1))
    store.cas_commit(task["id"], "input-required", "counter", own_u=0.8)
    assert store.get_task(task["id"])["round_at_turn_start"] is None


def test_startup_recovery_sets_round_at_turn_start(store: Store) -> None:
    task = store.create_task()
    store.begin_turn(task["id"], "u1", user_offer(0))
    assert store.get_task(task["id"])["round"] == 0

    assert store.startup_recovery() == 1
    row = store.get_task(task["id"])
    assert row["state"] == "input-required"
    assert row["round_at_turn_start"] == row["round"] == 0


def test_resume_turn_requires_round_at_turn_start(store: Store) -> None:
    task = store.create_task()
    with pytest.raises(StoreError):
        store.resume_turn(task["id"])

    store.begin_turn(task["id"], "u1", user_offer(0))
    store.startup_recovery()
    resumed = store.resume_turn(task["id"])
    assert resumed["state"] == "working"
    assert resumed["round"] == 0
    assert resumed["round_at_turn_start"] == 0


def test_complete_with_accept_builds_artifact_and_keeps_round(store: Store) -> None:
    task = store.create_task()
    store.begin_turn(task["id"], "u1", user_offer(0))
    store.append_message(task["id"], "a1", "agent", agent_counter(1))
    store.cas_commit(task["id"], "input-required", "counter", own_u=0.8)
    before = store.get_task(task["id"])["round"]

    store.complete_with_accept(task["id"], TERMS)
    row = store.get_task(task["id"])
    assert row["state"] == "completed"
    assert row["round"] == before
    artifacts = store.artifacts_for_task(task["id"])
    assert len(artifacts) == 1
    data = artifacts[0]["parts"][0]["data"]
    assert protocol.verify_agreement(task["id"], TERMS, data["sha256"])


def test_cancel_is_idempotent_and_terminal_states_are_not_cancelable(store: Store) -> None:
    task = store.create_task()
    canceled, error = store.cancel_task(task["id"])
    assert error is None
    assert canceled["state"] == "canceled"
    again, error = store.cancel_task(task["id"])
    assert error is None
    assert again["state"] == "canceled"

    done = store.create_task()
    store.begin_turn(done["id"], "u2", user_offer(0))
    store.cas_commit(done["id"], "completed", "accept")
    _, error = store.cancel_task(done["id"])
    assert error == config.A2A_TASK_NOT_CANCELABLE

    _, error = store.cancel_task("missing")
    assert error == config.A2A_TASK_NOT_FOUND


def test_task_view_reconstructs_status_message_and_clamps_history(store: Store) -> None:
    task = store.create_task()
    store.begin_turn(task["id"], "u1", user_offer(0))
    store.append_message(task["id"], "a1", "agent", agent_counter(1))

    view = store.task_view(task["id"], history_length=1)
    assert view is not None
    assert view["status"]["state"] == "TASK_STATE_WORKING"
    assert view["status"]["message"]["messageId"] == "a1"
    assert len(view["history"]) == 1
    assert view["history"][0]["messageId"] == "a1"

    clamped = store.task_view(task["id"], history_length=999)
    assert len(clamped["history"]) == 2
