"""Server tests: the R/G/C decision tables plus S1/S2 end-to-end.

Every test is in-process over an ASGI loopback: no ports, no network, no
sleeps. The buyer and seller both use the deterministic fallback in Phase 1.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.conftest import loopback_client, make_message

from src import config, protocol
from src.buyer_cli import negotiate
from src.client import A2AClient
from src.server import create_app
from src.store import Store
from src.utility import Profile

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
TERMS = {"price": 800, "delivery_days": 14, "revisions": 3}


def load_profile(name: str) -> Profile:
    return Profile.load(json.loads((ROOT / "profiles" / f"{name}.json").read_text()))


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "seller.db"), journal_mode="MEMORY")
    app = create_app(store, load_profile("seller"), card_url="http://testserver")
    try:
        yield SimpleNamespace(store=store, app=app, profile=load_profile("seller"))
    finally:
        store.close()


@pytest.fixture
async def http(env):
    async with loopback_client(env.app) as client:
        yield client


async def rpc(
    http,
    method: str,
    params: dict[str, Any] | None,
    request_id: int = 1,
) -> dict[str, Any]:
    response = await http.post(
        config.A2A_RPC_PATH,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    assert response.status_code == 200
    return response.json()


def offer(
    round_number: int = 0,
    *,
    terms: dict[str, int] | None = None,
    message_id: str = "m1",
    task_id: str | None = None,
    role: str = "ROLE_USER",
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if parts is None:
        parts = [
            protocol.data_part(
                {
                    "type": "offer",
                    "terms": dict(terms or TERMS),
                    "round": round_number,
                }
            )
        ]
    message = make_message(messageId=message_id, role=role, parts=parts)
    if task_id is not None:
        message["taskId"] = task_id
    return message


async def send(http, message: dict[str, Any]) -> dict[str, Any]:
    return await rpc(http, config.A2A_METHOD_SEND, {"message": message})


def error_code(body: dict[str, Any]) -> int:
    return int(body["error"]["code"])


async def first_exchange(http) -> tuple[dict[str, Any], dict[str, Any]]:
    body = await send(http, offer(0))
    task = body["result"]["task"]
    counter = task["history"][-1]
    return task, counter


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def test_agent_card_contract_and_no_private_values(http) -> None:
    response = await http.get(config.AGENT_CARD_PATH)
    assert response.status_code == 200
    card = response.json()
    for field in (
        "name",
        "description",
        "version",
        "supportedInterfaces",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ):
        assert field in card
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert card["supportedInterfaces"][0]["protocolVersion"] == config.A2A_PROTOCOL_VERSION

    blob = json.dumps(card)
    for private in ("600", "1000", "700", "1200", "reservation", "ideal"):
        assert private not in blob


# ---------------------------------------------------------------------------
# R0-R3: framing and validation
# ---------------------------------------------------------------------------


async def test_r0_body_too_large(http) -> None:
    huge = b"{" + b"x" * (config.MAX_MESSAGE_BYTES + 10) + b"}"
    response = await http.post(config.A2A_RPC_PATH, content=huge)
    assert response.status_code == 200
    assert error_code(response.json()) == config.JSONRPC_INVALID_REQUEST


async def test_r1_invalid_json(http) -> None:
    response = await http.post(
        config.A2A_RPC_PATH,
        content=b"{not-json",
        headers={"content-type": "application/json"},
    )
    assert error_code(response.json()) == config.JSONRPC_PARSE_ERROR


async def test_r2_envelope_and_method(http) -> None:
    bad_envelope = await http.post(
        config.A2A_RPC_PATH,
        json={"jsonrpc": "1.0", "id": 1, "method": "SendMessage", "params": {}},
    )
    assert error_code(bad_envelope.json()) == config.JSONRPC_INVALID_REQUEST

    unknown = await rpc(http, "NoSuchMethod", {})
    assert error_code(unknown) == config.JSONRPC_METHOD_NOT_FOUND

    not_an_object = await http.post(config.A2A_RPC_PATH, json=[1, 2, 3])
    assert error_code(not_an_object.json()) == config.JSONRPC_INVALID_REQUEST


@pytest.mark.parametrize(
    "bad_message",
    [
        # role must be user (R3)
        offer(0, role="ROLE_AGENT"),
        # exactly one data part
        {
            **offer(0),
            "parts": [
                protocol.data_part({"type": "offer", "terms": TERMS, "round": 0}),
                protocol.data_part({"type": "offer", "terms": TERMS, "round": 0}),
            ],
        },
        # at most one text part, and no raw/url parts
        {
            **offer(0),
            "parts": [
                protocol.data_part({"type": "offer", "terms": TERMS, "round": 0}),
                protocol.text_part("one"),
                protocol.text_part("two"),
            ],
        },
        {**offer(0), "parts": [{"raw": "aGVsbG8="}]},
        # strict ints
        {
            **offer(0),
            "parts": [
                protocol.data_part(
                    {"type": "offer", "terms": {**TERMS, "price": 800.0}, "round": 0}
                )
            ],
        },
        {
            **offer(0),
            "parts": [
                protocol.data_part({"type": "offer", "terms": TERMS, "round": "0"})
            ],
        },
        # rationale cap
        {
            **offer(0),
            "parts": [
                protocol.data_part({"type": "offer", "terms": TERMS, "round": 0}),
                protocol.text_part("x" * (config.MAX_RATIONALE_CHARS + 1)),
            ],
        },
        # missing messageId
        {**offer(0), "messageId": ""},
        # seller-to-server message types are not accepted from a buyer
        {
            **offer(0),
            "parts": [
                protocol.data_part({"type": "counter", "terms": TERMS, "round": 0})
            ],
        },
    ],
)
async def test_r3_invalid_send_params(http, bad_message: dict[str, Any]) -> None:
    body = await send(http, bad_message)
    assert error_code(body) == config.JSONRPC_INVALID_PARAMS


# ---------------------------------------------------------------------------
# R4/R4b: capacity
# ---------------------------------------------------------------------------


async def test_r4_capacity_and_r4b_concurrent_creators(env, http, monkeypatch) -> None:
    monkeypatch.setattr(config, "MAX_OPEN_TASKS", 1)

    env.store.create_task()
    blocked = await send(http, offer(0, message_id="blocked"))
    assert error_code(blocked) == config.JSONRPC_INVALID_PARAMS
    assert blocked["error"]["message"] == config.REASONS["R4"]

    # A taskless replay of a non-existent message must still be rejected.
    env.store.conn.execute("DELETE FROM tasks")
    env.store.conn.commit()

    first = asyncio.create_task(send(http, offer(0, message_id="race-a")))
    second = asyncio.create_task(send(http, offer(0, message_id="race-b")))
    bodies = await asyncio.gather(first, second)
    successes = [b for b in bodies if "result" in b]
    errors = [b for b in bodies if "error" in b]
    assert len(successes) == 1
    assert len(errors) == 1
    assert errors[0]["error"]["message"] == config.REASONS["R4"]


# ---------------------------------------------------------------------------
# R5/R6: existence, replay, in-flight, resume
# ---------------------------------------------------------------------------


async def test_r5_task_not_found(http) -> None:
    body = await send(http, offer(0, task_id="missing"))
    assert error_code(body) == config.A2A_TASK_NOT_FOUND


async def test_r6a_message_id_used_in_another_task(env, http) -> None:
    body = await send(http, offer(0, message_id="shared"))
    assert "result" in body
    other = env.store.create_task()
    conflict = await send(http, offer(0, message_id="shared", task_id=other["id"]))
    assert conflict["error"]["message"] == config.REASONS["R6A"]


async def test_r6b_replay_is_idempotent_with_and_without_task_id(env, http) -> None:
    message = offer(0, message_id="replay")
    first = (await send(http, message))["result"]["task"]
    count = len(env.store.messages_for_task(first["id"]))

    with_id = dict(message)
    with_id["taskId"] = first["id"]
    second = (await send(http, with_id))["result"]["task"]
    third = (await send(http, message))["result"]["task"]

    for replay in (second, third):
        assert replay["id"] == first["id"]
        assert replay["status"]["state"] == first["status"]["state"]
        assert [m["messageId"] for m in replay["history"]] == [
            m["messageId"] for m in first["history"]
        ]
        assert replay["artifacts"] == first["artifacts"]
    assert len(env.store.messages_for_task(first["id"])) == count


async def test_r6d_rejects_same_message_while_turn_is_running(env, http) -> None:
    task = env.store.create_task()
    env.store.begin_turn(task["id"], "inflight", offer(0)["parts"])
    body = await send(http, offer(0, message_id="inflight", task_id=task["id"]))
    assert body["error"]["message"] == config.REASONS["R6D"]


async def test_r6c_resumes_incomplete_turn_without_duplicate_message(env, http) -> None:
    task = env.store.create_task()
    env.store.begin_turn(task["id"], "resume", offer(0)["parts"])
    env.store.startup_recovery()
    assert env.store.get_task(task["id"])["state"] == "input-required"

    body = await send(http, offer(0, message_id="resume", task_id=task["id"]))
    resumed = body["result"]["task"]
    assert resumed["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert len([m for m in resumed["history"] if m["role"] == "ROLE_USER"]) == 1
    assert any(m["role"] == "ROLE_AGENT" for m in resumed["history"])
    assert env.store.get_task(task["id"])["round"] == 2


async def test_r6c_resume_drift_is_rejected(env, http) -> None:
    task = env.store.create_task()
    env.store.begin_turn(task["id"], "drift", offer(0)["parts"])
    env.store.startup_recovery()
    env.store.conn.execute(
        "UPDATE tasks SET round_at_turn_start = 4 WHERE id = ?", (task["id"],)
    )
    env.store.conn.commit()

    body = await send(http, offer(0, message_id="drift", task_id=task["id"]))
    assert body["error"]["message"] == config.REASONS["RESUME_DRIFT"]


# ---------------------------------------------------------------------------
# R7-R12b
# ---------------------------------------------------------------------------


async def test_r7_terminal_task_rejects_new_message(http) -> None:
    task, counter = await first_exchange(http)
    terms = counter["parts"][0]["data"]["terms"]
    accepted = await send(
        http,
        offer(
            2,
            message_id="accept",
            task_id=task["id"],
            parts=[
                protocol.data_part({"type": "accept", "terms": terms, "round": 2})
            ],
        ),
    )
    assert accepted["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    late = await send(http, offer(2, message_id="late", task_id=task["id"]))
    assert late["error"]["message"] == config.REASONS["R7"]


async def test_r8_busy_task_rejects_new_message(env, http) -> None:
    task = env.store.create_task()
    env.store.begin_turn(task["id"], "working", offer(0)["parts"])
    body = await send(http, offer(0, message_id="new", task_id=task["id"]))
    assert body["error"]["message"] == config.REASONS["R8"]


async def test_r9_round_mismatch(http) -> None:
    task, _ = await first_exchange(http)
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    body = await send(
        http, offer(1, message_id="wrong-round", task_id=task["id"])
    )
    assert body["error"]["message"] == config.REASONS["R9"]


async def test_r10_accept_mismatch(http) -> None:
    task, counter = await first_exchange(http)
    wrong = dict(counter["parts"][0]["data"]["terms"])
    wrong["price"] += 10
    body = await send(
        http,
        offer(
            2,
            message_id="bad-accept",
            task_id=task["id"],
            parts=[protocol.data_part({"type": "accept", "terms": wrong, "round": 2})],
        ),
    )
    assert body["error"]["message"] == config.REASONS["R10"]


async def test_r11_accept_completes_and_builds_verified_artifact(env, http) -> None:
    task, counter = await first_exchange(http)
    terms = counter["parts"][0]["data"]["terms"]
    before_round = env.store.get_task(task["id"])["round"]

    body = await send(
        http,
        offer(
            2,
            message_id="good-accept",
            task_id=task["id"],
            parts=[protocol.data_part({"type": "accept", "terms": terms, "round": 2})],
        ),
    )
    completed = body["result"]["task"]
    assert completed["status"]["state"] == "TASK_STATE_COMPLETED"
    assert env.store.get_task(task["id"])["round"] == before_round

    artifacts = env.store.artifacts_for_task(task["id"])
    assert len(artifacts) == 1
    data = artifacts[0]["parts"][0]["data"]
    assert data["terms"] == terms
    assert protocol.verify_agreement(task["id"], terms, data["sha256"])


async def test_r12_taskless_offer_creates_task_and_counter(http) -> None:
    body = await send(http, offer(0, message_id="new-task"))
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert [m["role"] for m in task["history"]] == ["ROLE_USER", "ROLE_AGENT"]
    counter = task["history"][-1]["parts"][0]["data"]
    assert counter["type"] == "counter"
    assert counter["round"] == 1
    assert counter["terms"] != TERMS


async def test_r12b_taskless_accept_is_rejected(http) -> None:
    body = await send(
        http,
        offer(
            0,
            message_id="taskless-accept",
            parts=[protocol.data_part({"type": "accept", "terms": TERMS, "round": 0})],
        ),
    )
    assert error_code(body) == config.JSONRPC_INVALID_PARAMS


async def test_r13_offer_on_input_required_starts_the_next_turn(env, http) -> None:
    task, _ = await first_exchange(http)
    assert env.store.get_task(task["id"])["round"] == 2

    body = await send(http, offer(2, message_id="next", task_id=task["id"]))
    advanced = body["result"]["task"]
    assert advanced["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert [m["parts"][0]["data"]["round"] for m in advanced["history"]] == [
        0,
        1,
        2,
        3,
    ]
    assert env.store.get_task(task["id"])["round"] == 4


async def test_seller_accept_turn_completes_with_an_artifact(env, http) -> None:
    strong = {"price": 1200, "delivery_days": 21, "revisions": 1}
    body = await send(http, offer(0, terms=strong, message_id="strong"))
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert env.store.get_task(task["id"])["round"] == 0
    last = task["history"][-1]
    assert last["role"] == "ROLE_AGENT"
    assert last["parts"][0]["data"]["type"] == "accept"
    data = task["artifacts"][0]["parts"][0]["data"]
    assert data["terms"] == strong
    assert protocol.verify_agreement(task["id"], strong, data["sha256"])


# ---------------------------------------------------------------------------
# G and C tables
# ---------------------------------------------------------------------------


async def test_g1_invalid_params(http) -> None:
    for params in (None, {}, {"id": ""}, {"id": 7}, {"id": "x", "historyLength": 1.5}):
        body = await rpc(http, config.A2A_METHOD_GET_TASK, params)
        assert error_code(body) == config.JSONRPC_INVALID_PARAMS


async def test_g2_task_not_found(http) -> None:
    body = await rpc(http, config.A2A_METHOD_GET_TASK, {"id": "missing"})
    assert error_code(body) == config.A2A_TASK_NOT_FOUND


async def test_g3_history_length_is_clamped(http) -> None:
    task, _ = await first_exchange(http)
    body = await rpc(
        http,
        config.A2A_METHOD_GET_TASK,
        {"id": task["id"], "historyLength": 999},
    )
    result = body["result"]
    assert len(result["history"]) <= config.MAX_HISTORY_RESPONSE
    assert result["status"]["message"]["messageId"] == task["history"][-1]["messageId"]


async def test_c1_c2_invalid_and_missing_task(http) -> None:
    for params in (None, {}, {"id": ""}, {"id": 3}):
        body = await rpc(http, config.A2A_METHOD_CANCEL_TASK, params)
        assert error_code(body) == config.JSONRPC_INVALID_PARAMS
    body = await rpc(http, config.A2A_METHOD_CANCEL_TASK, {"id": "missing"})
    assert error_code(body) == config.A2A_TASK_NOT_FOUND


async def test_c3_c5_cancel_is_idempotent(http) -> None:
    task, _ = await first_exchange(http)
    first = await rpc(http, config.A2A_METHOD_CANCEL_TASK, {"id": task["id"]})
    assert first["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    again = await rpc(http, config.A2A_METHOD_CANCEL_TASK, {"id": task["id"]})
    assert again["result"]["status"]["state"] == "TASK_STATE_CANCELED"


async def test_c4_terminal_task_is_not_cancelable(http) -> None:
    task, counter = await first_exchange(http)
    terms = counter["parts"][0]["data"]["terms"]
    await send(
        http,
        offer(
            2,
            message_id="done",
            task_id=task["id"],
            parts=[protocol.data_part({"type": "accept", "terms": terms, "round": 2})],
        ),
    )
    body = await rpc(http, config.A2A_METHOD_CANCEL_TASK, {"id": task["id"]})
    assert error_code(body) == config.A2A_TASK_NOT_CANCELABLE


# ---------------------------------------------------------------------------
# Auth (S15 basic)
# ---------------------------------------------------------------------------


async def test_bearer_auth_required_when_configured(tmp_path) -> None:
    store = Store(str(tmp_path / "auth.db"), journal_mode="MEMORY")
    app = create_app(
        store, load_profile("seller"), token="secret", card_url="http://testserver"
    )
    try:
        async with loopback_client(app) as http:
            unauthorized = await http.post(
                config.A2A_RPC_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {}},
            )
            assert unauthorized.status_code == 401

            wrong = await http.post(
                config.A2A_RPC_PATH,
                headers={"Authorization": "Bearer nope"},
                json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {}},
            )
            assert wrong.status_code == 401

            card = await http.get(config.AGENT_CARD_PATH)
            assert "secret" not in json.dumps(card.json())
    finally:
        store.close()


# ---------------------------------------------------------------------------
# S1/S2 end-to-end
# ---------------------------------------------------------------------------


def _offer_data(messages: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] != role:
            continue
        for part in message["parts"]:
            data = part.get("data")
            if isinstance(data, dict) and data.get("type") in {"offer", "counter"}:
                out.append(data)
    return out


async def test_s1_full_negotiation_completes_with_verified_artifact(env) -> None:
    buyer = load_profile("buyer")
    seller = load_profile("seller")
    async with loopback_client(env.app) as loop:
        client = A2AClient("http://testserver", http_client=loop)
        result = await negotiate(client, buyer, verbose=False)

    assert result["outcome"] == "completed"
    tid = result["task_id"]
    task = env.store.task_view(tid)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"]
    data = task["artifacts"][0]["parts"][0]["data"]
    assert protocol.verify_agreement(tid, data["terms"], data["sha256"])

    buyer_offers = _offer_data(task["history"], "ROLE_USER")
    seller_offers = _offer_data(task["history"], "ROLE_AGENT")
    assert [o["round"] for o in buyer_offers] == [0, 2, 4, 6]
    assert [o["round"] for o in seller_offers] == [1, 3, 5, 7]
    for data in buyer_offers:
        assert buyer.feasible(data["terms"])
    for data in seller_offers:
        assert seller.feasible(data["terms"])
    assert all(o["round"] <= config.MAX_ROUNDS for o in buyer_offers + seller_offers)


NO_ZOPA_BUYER = {
    "name": "buyer",
    "beta": 2.0,
    "issues": {
        "price": {"direction": "min", "ideal": 300, "reservation": 500, "weight": 0.5},
        "delivery_days": {
            "direction": "min",
            "ideal": 7,
            "reservation": 30,
            "weight": 0.2,
        },
        "revisions": {"direction": "max", "ideal": 5, "reservation": 1, "weight": 0.3},
    },
}


async def test_s2_no_zopa_ends_rejected_without_artifact(env) -> None:
    buyer = Profile.load(NO_ZOPA_BUYER)
    seller = load_profile("seller")
    async with loopback_client(env.app) as loop:
        client = A2AClient("http://testserver", http_client=loop)
        result = await negotiate(client, buyer, verbose=False)

    assert result["outcome"] == "rejected"
    tid = result["task_id"]
    task = env.store.task_view(tid)
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert task["artifacts"] == []
    for data in _offer_data(task["history"], "ROLE_USER"):
        assert buyer.feasible(data["terms"])
    for data in _offer_data(task["history"], "ROLE_AGENT"):
        assert seller.feasible(data["terms"])
