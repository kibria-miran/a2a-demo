"""Guards for bugs promoted out of evals/reviews (E-57).

Each entry below documents a past defect that tests must keep failing on
when the fix is reverted. The standing rule: discovered bug -> runbook
section + this file, same commit as the fix.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.conftest import loopback_client, make_message

from src import config, protocol, trace
from src.decision import Decision
from src.server import create_app
from src.store import Store
from src.utility import Profile

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]


def load_profile(name: str) -> Profile:
    return Profile.load(json.loads((ROOT / "profiles" / f"{name}.json").read_text()))


def test_peer_text_reaches_responder_on_normal_turn(tmp_path: Any) -> None:
    """A normal (non-resume) turn must hand the peer's text to the agent.

    Regression for: server stripped the message down to its data part
    before spawning the turn, so the peer's rationale never reached the
    proposer prompt (the resume path attached `_peer_text` correctly).
    """

    captured: dict[str, Any] = {}

    def capturing_responder(
        view: dict[str, Any],
        incoming: dict[str, Any],
        profile: Profile,
        own_history: list[float],
    ) -> Decision:
        captured.update(incoming)
        return Decision(
            kind="counter",
            terms={"price": 900, "delivery_days": 14, "revisions": 3},
            rationale="ok",
            round=1,
            own_u=0.5,
            source="code",
        )

    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(
        store,
        load_profile("seller"),
        responder=capturing_responder,
        card_url="http://testserver",
    )
    try:
        message = make_message(
            messageId="peer-text-1",
            parts=[
                protocol.data_part(
                    {
                        "type": "offer",
                        "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                        "round": 0,
                    }
                ),
                protocol.text_part("my budget is flexible-ish"),
            ],
        )
        env = SimpleNamespace(app=app)
        import asyncio

        async def run() -> dict[str, Any]:
            async with loopback_client(env.app) as client:
                response = await client.post(
                    "/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "SendMessage",
                        "params": {"message": message},
                    },
                )
                assert response.status_code == 200
                return response.json()

        body = asyncio.run(run())
        assert "error" not in body
    finally:
        store.close()

    assert captured.get("_peer_text") == "my budget is flexible-ish"


async def _send(http: Any, body: dict[str, Any]) -> dict[str, Any]:
    response = await http.post(config.A2A_RPC_PATH, json=body)
    assert response.status_code == 200
    return response.json()


def test_replay_of_accept_returns_the_completed_task(tmp_path: Any) -> None:
    """Replaying an accept's messageId must return the task (R6b),
    not a terminal-state error.

    Regression for: the buyer's accept message was never stored, so the
    replay path saw no stored message and fell through to R7 (-32602
    "task is terminal").
    """

    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(store, load_profile("seller"), card_url="http://testserver")
    try:
        async def run() -> None:
            async with loopback_client(app) as http:
                # Open the task; seller counters by code fallback.
                offer = make_message(
                    messageId="accept-replay-1",
                    parts=[
                        protocol.data_part(
                            {
                                "type": "offer",
                                "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                                "round": 0,
                            }
                        )
                    ],
                )
                first = await _send(
                    http, {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": offer}}
                )
                assert "error" not in first
                task = first["result"]["task"]
                history = task["history"]
                counter = next(
                    m for m in history if m["role"] == "ROLE_AGENT"
                )
                counter_data = next(
                    part["data"] for part in counter["parts"] if "data" in part
                )
                accept = make_message(
                    messageId="accept-replay-accept-1",
                    taskId=task["id"],
                    parts=[
                        protocol.data_part(
                            {
                                "type": "accept",
                                "terms": counter_data["terms"],
                                "round": counter_data["round"] + 1,
                            }
                        )
                    ],
                )
                accepted = await _send(
                    http, {"jsonrpc": "2.0", "id": 2, "method": "SendMessage", "params": {"message": accept}}
                )
                assert "error" not in accepted
                assert accepted["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

                replay = await _send(
                    http, {"jsonrpc": "2.0", "id": 3, "method": "SendMessage", "params": {"message": accept}}
                )
                assert "error" not in replay
                assert replay["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

        import asyncio

        asyncio.run(run())
    finally:
        store.close()


def test_turn_crash_sets_failed(tmp_path: Any) -> None:
    """A crashing turn must CAS-commit `failed` (E-22).

    Regression guard for the class of bugs where a responder raising
    leaves the task stuck in `working`.
    """

    def exploding(
        view: dict[str, Any],
        incoming: dict[str, Any],
        profile: Profile,
        own_history: list[float],
    ) -> Any:
        raise ValueError("boom")

    trace_path = tmp_path / "trace.jsonl"
    trace.configure(trace_path)
    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(
        store,
        load_profile("seller"),
        responder=exploding,
        card_url="http://testserver",
    )
    try:
        message = make_message(
            messageId="crash-offer-1",
            parts=[
                protocol.data_part(
                    {
                        "type": "offer",
                        "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                        "round": 0,
                    }
                )
            ],
        )

        async def run() -> dict[str, Any]:
            async with loopback_client(app) as http:
                return await _send(
                    http,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "SendMessage",
                        "params": {"message": message},
                    },
                )

        import asyncio

        body = asyncio.run(run())
    finally:
        store.close()
        trace.reset()

    assert body["result"]["task"]["status"]["state"] == "TASK_STATE_FAILED"
    events = [
        json.loads(line)
        for line in trace_path.read_text().splitlines()
        if line.strip()
    ]
    assert any(
        event.get("kind") == "turn_failed"
        and event.get("severity") == "error"
        and event.get("error_kind") == "turn_failed"
        for event in events
    )


def test_pushover_counter_rejected_by_band(tmp_path: Any) -> None:
    """An LLM counter that gives the peer a gift must fail V3 (band).

    Section 15 promotion: "LLM acts like a pushover: the band (V3) is the
    cure, not prompt words."
    """

    from src.negotiator import REASON_V3_BAND, validate_proposal

    seller = Profile.load(
        json.loads(
            (Path(__file__).resolve().parents[1] / "profiles" / "seller.json").read_text()
        )
    )
    pushover = {"price": 720, "delivery_days": 10, "revisions": 3}
    ok, reason = validate_proposal(
        {"terms": pushover, "rationale": "I will give it to you."},
        seller,
        1,
        [],
    )
    assert not ok
    assert reason == REASON_V3_BAND


def test_late_llm_answer_discarded_after_cancel(tmp_path: Any) -> None:
    """A counter arriving after a cancel must never overwrite `canceled`.

    Section 15 promotion: "Cancel then a late LLM answer overwrites
    canceled: CAS commit (D-15)."
    """
    import time

    entered = threading.Event()

    def slow_responder(
        view: dict[str, Any],
        incoming: dict[str, Any],
        profile: Any,
        own_history: list[float],
    ) -> Decision:
        entered.set()
        time.sleep(0.6)
        return Decision(
            kind="counter",
            terms={"price": 1150, "delivery_days": 19, "revisions": 1},
            rationale="too late",
            round=1,
            own_u=0.91,
            source="llm",
        )

    trace_path = tmp_path / "trace.jsonl"
    trace.configure(trace_path)
    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(
        store,
        load_profile("seller"),
        responder=slow_responder,
        card_url="http://testserver",
    )

    try:
        message = make_message(
            messageId="cancel-race-1",
            parts=[
                protocol.data_part(
                    {
                        "type": "offer",
                        "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                        "round": 0,
                    }
                )
            ],
        )

        async def run() -> str:
            async with loopback_client(app) as http:
                send = asyncio.create_task(
                    http.post(
                        config.A2A_RPC_PATH,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "SendMessage",
                            "params": {"message": message},
                        },
                    )
                )
                while not entered.is_set():
                    await asyncio.sleep(0.01)
                with sqlite3.connect(tmp_path / "seller.db") as conn:
                    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
                cancel = await http.post(
                    config.A2A_RPC_PATH,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "CancelTask",
                        "params": {"id": task_id},
                    },
                )
                assert cancel.status_code == 200
                await send
                return str(task_id)

        task_id = asyncio.run(run())
        task = store.task_view(task_id)
        assert task is not None
        assert task["status"]["state"] == "TASK_STATE_CANCELED"
    finally:
        store.close()
        trace.reset()

    events = [
        json.loads(line)
        for line in trace_path.read_text().splitlines()
        if line.strip()
    ]
    assert any(event.get("kind") == "discarded" for event in events)
    assert not any(
        event.get("direction") == "out" and event.get("kind") == "counter" for event in events
    )


def test_spec_names_stay_in_pinned_modules() -> None:
    """A2A wire names must not drift into the rest of the codebase.

    Section 15 promotion: "Spec drift: keep A2A names only in config.py
    and protocol.py."
    """
    assert frozenset({"SendMessage", "GetTask", "CancelTask"}) == config.A2A_METHODS
    assert config.A2A_PROTOCOL_VERSION == "1.0"
    assert config.AGENT_CARD_PATH == "/.well-known/agent-card.json"
    assert config.A2A_RPC_PATH == "/"


def test_sync_responder_does_not_block_event_loop(tmp_path: Any) -> None:
    """A sync (blocking) responder must run in a worker thread (E-4).

    Regression for: llm.chat() is synchronous; called directly from the
    async turn it monopolized the event loop, starving tasks/get and
    delaying the BLOCKING_TIMEOUT snapshot.
    """

    def blocking_responder(
        view: dict[str, Any],
        incoming: dict[str, Any],
        profile: Profile,
        own_history: list[float],
    ) -> Decision:
        time.sleep(1.0)
        return Decision(
            kind="counter",
            terms={"price": 1150, "delivery_days": 19, "revisions": 1},
            rationale="slow",
            round=1,
            own_u=0.9,
            source="llm",
        )

    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(
        store, load_profile("seller"), responder=blocking_responder, card_url="http://testserver"
    )
    try:
        async def run() -> None:
            async with loopback_client(app) as http:
                message = make_message(
                    messageId="blocking-responder-1",
                    parts=[
                        protocol.data_part(
                            {
                                "type": "offer",
                                "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                                "round": 0,
                            }
                        )
                    ],
                )
                send = asyncio.create_task(
                    http.post(
                        config.A2A_RPC_PATH,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "SendMessage",
                            "params": {"message": message},
                        },
                    )
                )
                await asyncio.sleep(0.1)  # let the handler start
                started = time.monotonic()
                card = await http.get(config.AGENT_CARD_PATH)
                elapsed = time.monotonic() - started
                assert card.status_code == 200
                assert elapsed < 0.5, "card GET must not wait for the turn"
                await send

        import asyncio

        asyncio.run(run())
    finally:
        store.close()


def test_fallback_stays_within_eps_of_target() -> None:
    """Fallback offers track target(T) within EPS_ROUNDING for all rounds.

    Section 15 promotion: "U drifts off target: the lattice search and the
    EPS constants solve it; never loosen EPS to hide a bug."
    """
    from src import config
    from src.negotiator import fallback_offer
    from src.utility import Profile

    for name in ("buyer", "seller"):
        profile = Profile.load(
            json.loads((Path(__file__).resolve().parents[1] / "profiles" / f"{name}.json").read_text())
        )
        history: list[float] = []
        for round_number in range(0, config.MAX_ROUNDS + 1):
            terms = fallback_offer(profile, round_number, history)
            utility = profile.utility(terms)
            target = profile.target_utility(min(round_number, config.MAX_ROUNDS))
            assert abs(utility - target) <= config.EPS_ROUNDING
            history.append(utility)


def test_replay_never_reruns_finishing_turn(tmp_path: Any) -> None:
    """Sending the same messageId three times never runs a second turn.

    Section 15 promotion: "Duplicate turns after a timeout: always retry
    with the SAME messageId."
    """
    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(store, load_profile("seller"), card_url="http://testserver")
    message = make_message(
        messageId="replay-triple",
        parts=[
            protocol.data_part(
                {
                    "type": "offer",
                    "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                    "round": 0,
                }
            )
        ],
    )

    async def run() -> list[dict[str, Any]]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {"message": message},
        }
        async with loopback_client(app) as http:
            out = []
            for _ in range(3):
                out.append(await _send(http, body))
            return out

    try:
        bodies = asyncio.run(run())
    finally:
        store.close()

    tasks = [body["result"]["task"] for body in bodies]
    assert len({task["id"] for task in tasks}) == 1
    assert [body["result"]["task"] for body in bodies] == [tasks[0]] * 3


def test_resume_drift_is_rejected_with_32602(tmp_path: Any) -> None:
    """A resumed turn whose stored round drifted from round_at_turn_start
    must be rejected with RESUME_DRIFT (R6c), never resumed.

    Section 15 promotion: "Resume fails with 'resume round drift'" —
    the precondition is a non-NULL round_at_turn_start matching the
    stored message's round.
    """

    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(store, load_profile("seller"), card_url="http://testserver")
    task = store.create_task(None)
    parts = [
        protocol.data_part(
            {
                "type": "offer",
                "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
                "round": 0,
            }
        )
    ]
    store.begin_turn(task["id"], "drift", parts)
    store.startup_recovery()
    # Simulate the drift: round_at_turn_start no longer matches the
    # stored message's round (0).
    store.conn.execute(
        "UPDATE tasks SET round_at_turn_start = 4 WHERE id = ?", (task["id"],)
    )
    store.conn.commit()
    replayed = make_message(
        messageId="drift",
        taskId=task["id"],
        parts=parts,
    )

    async def run() -> dict[str, Any]:
        async with loopback_client(app) as http:
            return await _send(
                http,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "SendMessage",
                    "params": {"message": replayed},
                },
            )

    try:
        body = asyncio.run(run())
    finally:
        store.close()

    assert body["error"]["code"] == config.JSONRPC_INVALID_PARAMS
    assert body["error"]["message"] == config.REASONS["RESUME_DRIFT"]


def test_counter_advances_round_by_two_on_counter(tmp_path: Any) -> None:
    """CAS commit advances tasks.round by exactly 2 on a counter (E-47).

    Section 15 promotion: "Round symmetry breaks at the buyer's second
    offer (round 2 rejected by R9)."
    """
    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    task = store.create_task(None)
    offer = {
        "type": "offer",
        "terms": {"price": 800, "delivery_days": 14, "revisions": 3},
        "round": 0,
    }
    store.begin_turn(task["id"], "rt-1", [{"data": offer}])
    committed = store.cas_commit(
        task["id"],
        "input-required",
        "counter",
        own_u=0.9,
        agent_message_id="agent-1",
        agent_parts=[{"data": offer | {"type": "counter", "round": 1}}],
    )
    assert committed is True
    assert store.get_task(task["id"])["round"] == 2
    store.close()


def test_v5_private_value_leak_blocked() -> None:
    """An LLM rationale containing a private number fails validation (V5).

    Section 15 promotion: "Rationale leaks a private number."
    """
    from src.negotiator import REASON_V5_LEAK, validate_proposal
    from src.utility import Profile

    profile = Profile.load(
        json.loads(
            (Path(__file__).resolve().parents[1] / "profiles" / "seller.json").read_text()
        )
    )
    proposal = {
        "terms": {"price": 1100, "delivery_days": 21, "revisions": 1},
        "rationale": "my reservation is 700",
    }
    ok, reason = validate_proposal(proposal, profile, 0, [])
    assert not ok
    assert reason == REASON_V5_LEAK
