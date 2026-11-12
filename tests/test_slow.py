"""Slow tier: real uvicorn seller subprocess + real fake-NIM subprocess.

S9   kill -9 the seller mid-turn; restart; the buyer retries the same
     messageId (R6c resume); the negotiation completes with no duplicated
     messages.
S10  concurrency against the real seller subprocess: (a) two simultaneous
     SendMessage with the SAME messageId produce exactly one task; (b) two
     simultaneous SendMessage with different messageIds on one task: one
     wins, the other gets -32602 "task is busy".
S14  fake NIM sleeps past LLM_TIMEOUT on both proposer attempts, so the
     turn outlives BLOCKING_TIMEOUT; the timed-out reply already carries a
     task id (E-26) and the client can finish via tasks/get polling.

Budget: S9 ~5 s, S10 ~6 s, S14 ~62 s. The slow tier is opt-in: `make slow`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from src import config, protocol

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
TERMS = {"price": 800, "delivery_days": 14, "revisions": 3}


# ---------------------------------------------------------------------------
# Fixtures: fake NIM + seller subprocesses
# ---------------------------------------------------------------------------

class FakeNIM:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "fake_nim_server.py")],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        self.port = int(self.proc.stdout.readline())
        self.url = f"http://127.0.0.1:{self.port}"
        # The port is printed before uvicorn binds; wait for it to listen.
        for _ in range(100):
            try:
                with httpx.Client(base_url=self.url, timeout=1.0) as client:
                    client.get("/__state")
                break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise TimeoutError("fake NIM did not come up")

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(base_url=self.url, timeout=10.0) as client:
            return client.post(path, json=payload).json()

    def get(self, path: str) -> dict[str, Any]:
        with httpx.Client(base_url=self.url, timeout=10.0) as client:
            return client.get(path).json()

    def stage(self, responses: list[str]) -> None:
        self.post("/__stage", {"responses": responses})

    def control(self, sleep_calls: dict[int, float]) -> None:
        self.post("/__control", {"sleep_calls": {str(k): v for k, v in sleep_calls.items()}})

    def state(self) -> dict[str, Any]:
        return self.get("/__state")

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=5)


def proposal(terms: dict[str, int], rationale: str) -> str:
    return json.dumps({"terms": terms, "rationale": rationale})


# Seller's round-1 counter: feasible, inside the seller's band.
SELLER_COUNTER = {"price": 1150, "delivery_days": 19, "revisions": 1}
# Seller's round-3 counter after the resumed turn.
SELLER_COUNTER_2 = {"price": 1120, "delivery_days": 18, "revisions": 2}


def _free_port() -> int:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_seller(db: Path, fake_nim: FakeNIM) -> _SellerProcess:
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "NIM_BASE_URL": f"http://127.0.0.1:{fake_nim.port}/v1",
            "NIM_MODEL": "fake-model",
            "NVIDIA_API_KEY": "dummy",
            "A2A_TRACE_PATH": str(db.parent / f"{db.stem}.trace.jsonl"),
        }
    )
    seller = _SellerProcess(db, port, env)
    _wait_for_card(seller.url, timeout=15.0)
    return seller


class _SellerProcess:
    def __init__(self, db: Path, port: int, env: dict[str, str]) -> None:
        self.db = db
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "src.main_seller",
                "--db",
                str(db),
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=5)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


def _wait_for_card(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with httpx.Client(base_url=url, timeout=2.0) as client:
                response = client.get(config.AGENT_CARD_PATH)
                if response.status_code == 200:
                    return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise TimeoutError(f"seller did not come up at {url}")


def rpc(url: str, method: str, params: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    with httpx.Client(base_url=url, timeout=timeout) as client:
        response = client.post(
            config.A2A_RPC_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        return response.json()


def offer_message(
    message_id: str,
    round_number: int,
    terms: dict[str, int],
    task_id: str | None = None,
    msg_type: str = "offer",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [
            protocol.data_part({"type": msg_type, "terms": dict(terms), "round": round_number}),
        ],
    }
    if task_id is not None:
        message["taskId"] = task_id
    return message


@pytest.fixture
def fake_nim():
    fake = FakeNIM()
    yield fake
    fake.close()


# ---------------------------------------------------------------------------
# S9: kill -9 mid-turn, restart, resume, negotiation completes
# ---------------------------------------------------------------------------


def test_s9_kill9_midturn_restart_resumes(tmp_path: Path, fake_nim: FakeNIM) -> None:
    fake_nim.stage(
        [
            proposal(SELLER_COUNTER, "Counter."),      # turn 1 (round 0 -> 1)
            proposal(SELLER_COUNTER_2, "Still on."),  # killed turn pops this; never completes
            proposal(SELLER_COUNTER_2, "Still on."),  # resumed turn (round 2 -> 3)
        ]
    )
    db = tmp_path / "seller.db"
    seller = start_seller(db, fake_nim)
    try:
        first = rpc(seller.url, "SendMessage", {"message": offer_message("mA", 0, TERMS)})
        task = first["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        task_id = task["id"]

        fake_nim.control({2: 8.0})  # sleep the SECOND llm call (the next turn)
        second_body = {
            "message": offer_message("mB", 2, {"price": 780, "delivery_days": 12, "revisions": 2}, task_id)
        }

        async def send_then_kill() -> None:
            async with httpx.AsyncClient(base_url=seller.url, timeout=5.0) as client:
                with pytest.raises(httpx.HTTPError):
                    await client.post(
                        config.A2A_RPC_PATH,
                        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": second_body},
                    )

        async def kill_soon() -> None:
            await asyncio.sleep(1.5)
            seller.kill()

        async def both() -> None:
            await asyncio.gather(send_then_kill(), kill_soon())

        asyncio.run(both())

        # Restart against the same DB; startup recovery (T8) runs at boot.
        seller = start_seller(db, fake_nim)
        with sqlite3.connect(db) as conn:
            state = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        assert state == "input-required"

        resumed = rpc(seller.url, "SendMessage", second_body)
        assert "error" not in resumed, resumed
        resumed_task = resumed["result"]["task"]
        assert resumed_task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

        # Accept the resumed counter; the task completes and hash verifies.
        counter_terms = SELLER_COUNTER_2
        accept_body = {
            "message": offer_message("mC", 4, counter_terms, task_id, msg_type="accept")
        }
        accepted = rpc(seller.url, "SendMessage", accept_body)
        final = accepted["result"]["task"]
        assert final["status"]["state"] == "TASK_STATE_COMPLETED"
        artifact = final["artifacts"][0]
        artifact_data = next(p["data"] for p in artifact["parts"] if "data" in p)
        assert protocol.verify_agreement(task_id, artifact_data["terms"], artifact_data["sha256"])

        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT role, seq FROM messages WHERE task_id = ? ORDER BY seq", (task_id,)
            ).fetchall()
        user_count = sum(1 for role, _ in rows if role == "user")
        assert user_count == 3  # A, B once, C — no duplicated resume
        assert len(rows) == 5  # 3 user + 2 agent (counter round 1, counter round 3)
    finally:
        seller.close()


# ---------------------------------------------------------------------------
# S10: concurrency
# ---------------------------------------------------------------------------


def test_s10a_concurrent_same_message_id_one_task(tmp_path: Path, fake_nim: FakeNIM) -> None:
    db = tmp_path / "seller.db"
    seller = start_seller(db, fake_nim)
    try:
        message = offer_message("dup-id", 0, TERMS)

        async def run() -> list[dict[str, Any]]:
            async with httpx.AsyncClient(base_url=seller.url, timeout=90.0) as client:
                body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "SendMessage",
                    "params": {"message": message},
                }
                return await asyncio.gather(
                    client.post(config.A2A_RPC_PATH, json=body),
                    client.post(config.A2A_RPC_PATH, json=body),
                )

        responses = asyncio.run(run())
        bodies = [r.json() for r in responses]
        ok = [body for body in bodies if "error" not in body]
        errors = [body for body in bodies if "error" in body]
        # One winner creates the task; the loser either replays it (R6b)
        # or, while the winner's turn is still in flight, gets R6d.
        assert len(ok) + len(errors) == 2
        assert ok, "at least one response must succeed"
        for body in errors:
            assert body["error"]["code"] == config.JSONRPC_INVALID_PARAMS
            assert body["error"]["message"] == config.REASONS["R6D"]
        task_ids = {body["result"]["task"]["id"] for body in ok}
        assert len(task_ids) == 1
        with sqlite3.connect(db) as conn:
            agent_rows = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role = 'agent'"
            ).fetchone()[0]
            user_rows = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role = 'user'"
            ).fetchone()[0]
        assert agent_rows == 1
        assert user_rows == 1
    finally:
        seller.close()


def test_s10b_different_message_ids_second_gets_r8(
    tmp_path: Path, fake_nim: FakeNIM
) -> None:
    fake_nim.control({1: 5.0})  # first turn's LLM sleeps 5 s
    db = tmp_path / "seller.db"
    seller = start_seller(db, fake_nim)
    try:
        message_a = offer_message("busy-A", 0, TERMS)
        body_a = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {"message": message_a},
        }

        async def run() -> dict[str, Any]:
            async with httpx.AsyncClient(base_url=seller.url, timeout=90.0) as client:
                first = asyncio.create_task(client.post(config.A2A_RPC_PATH, json=body_a))
                await asyncio.sleep(1.0)
                # Discover the task id from the DB while turn A is sleeping.
                with sqlite3.connect(db) as conn:
                    task_id = conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
                body_b = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "SendMessage",
                    "params": {"message": offer_message("busy-B", 0, TERMS, task_id)},
                }
                second = await client.post(config.A2A_RPC_PATH, json=body_b)
                first_done = await first
                return {"second": second.json(), "first": first_done.json()}

        result = asyncio.run(run())
        assert result["second"]["error"]["code"] == config.JSONRPC_INVALID_PARAMS
        assert result["second"]["error"]["message"] == config.REASONS["R8"]
        assert "error" not in result["first"]
        assert result["first"]["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    finally:
        seller.close()


# ---------------------------------------------------------------------------
# S14: turn outlives BLOCKING_TIMEOUT; the timed-out reply carries a task id
# ---------------------------------------------------------------------------


def test_s14_blocking_timeout_yields_task_id(tmp_path: Path, fake_nim: FakeNIM) -> None:
    fake_nim.control({1: 35.0, 2: 35.0})  # both attempts exceed LLM_TIMEOUT (30 s)
    db = tmp_path / "seller.db"
    seller = start_seller(db, fake_nim)
    try:
        message = offer_message("timeout-offer", 0, TERMS)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {"message": message},
        }
        started = time.monotonic()
        with httpx.Client(base_url=seller.url, timeout=config.BLOCKING_TIMEOUT + 30) as client:
            response = client.post(config.A2A_RPC_PATH, json=body).json()
        elapsed = time.monotonic() - started

        # The snapshot reply came back at (roughly) BLOCKING_TIMEOUT.
        assert elapsed >= config.BLOCKING_TIMEOUT - 3
        task = response["result"]["task"]  # state still working
        assert task["status"]["state"] == "TASK_STATE_WORKING"
        assert task["id"]

        # ...and the turn eventually committed fallback (both LLM calls died
        # at the 30 s client timeout); the same task reaches input-required.
        deadline = time.monotonic() + 40
        final = None
        while time.monotonic() < deadline:
            with httpx.Client(base_url=seller.url, timeout=10.0) as client:
                final = client.post(
                    config.A2A_RPC_PATH,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "GetTask",
                        "params": {"id": task["id"]},
                    },
                ).json()["result"]
            if final["status"]["state"] != "TASK_STATE_WORKING":
                break
            time.sleep(2.0)
        assert final is not None
        assert final["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

        with sqlite3.connect(db) as conn:
            user_rows = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role = 'user'"
            ).fetchone()[0]
        assert user_rows == 1
    finally:
        seller.close()
