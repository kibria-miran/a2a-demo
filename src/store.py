"""SQLite persistence, per-task locks and the CAS commit (sections 7, 8.5).

One Store instance owns one SQLite connection. All methods are synchronous;
the server serializes per-task work with the asyncio locks exposed here.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from typing import Any

from src import clock, config, protocol, state_machine

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    context_id           TEXT,
    state                TEXT NOT NULL,
    round                INTEGER NOT NULL DEFAULT 0,
    round_at_turn_start  INTEGER,
    own_utility_history  TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(state)
    WHERE state NOT IN ('completed','canceled','failed','rejected');

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    message_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    parts_json  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(task_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_user_message_id
    ON messages(message_id) WHERE role = 'user';

CREATE TABLE IF NOT EXISTS artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    artifact_id  TEXT NOT NULL,
    name         TEXT,
    parts_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

_AGENT_TYPES = {"counter", "accept", "walk_away"}


class StoreError(Exception):
    pass


def _now_iso() -> str:
    return clock.to_iso(clock.now())


class Store:
    def __init__(self, path: str, *, journal_mode: str = "WAL") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA journal_mode={journal_mode}")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._locks: dict[str, asyncio.Lock] = {}
        self.creation_lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def lock_for(self, task_id: str) -> asyncio.Lock:
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock

    # -- task rows ----------------------------------------------------------

    def create_task(self, context_id: str | None = None) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        now = _now_iso()
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks (id, context_id, state, round, round_at_turn_start,"
                " own_utility_history, created_at, updated_at)"
                " VALUES (?, ?, 'submitted', 0, NULL, '[]', ?, ?)",
                (task_id, context_id, now, now),
            )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def open_task_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE state NOT IN"
            " ('completed','canceled','failed','rejected')"
        ).fetchone()
        return int(row["n"])

    def own_utility_history(self, task_id: str) -> list[float]:
        task = self.get_task(task_id)
        if task is None:
            return []
        return [float(v) for v in json.loads(task["own_utility_history"])]

    # -- messages -----------------------------------------------------------

    @staticmethod
    def _check_agent_parts(parts: list[dict[str, Any]], role: str) -> None:
        if role != "agent":
            return
        data_parts = [p for p in parts if p.get("data") is not None]
        text_parts = [p for p in parts if p.get("text") is not None]
        if len(data_parts) != 1 or len(text_parts) > 1:
            raise StoreError("agent message must have one data part and at most one text part")
        data = data_parts[0]["data"]
        if not isinstance(data, dict):
            raise StoreError("agent data part must be an object")
        offer = protocol.OfferData.model_validate(data)
        if offer.type not in _AGENT_TYPES:
            raise StoreError(
                "agent message data type must be counter, accept or walk_away"
            )

    def _insert_message(
        self,
        task_id: str,
        message_id: str,
        role: str,
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._check_agent_parts(parts, role)
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        seq = int(row["seq"]) + 1
        self.conn.execute(
            "INSERT INTO messages (task_id, message_id, role, parts_json, seq, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, message_id, role, json.dumps(parts), seq, _now_iso()),
        )
        stored = self.conn.execute(
            "SELECT * FROM messages WHERE task_id = ? AND seq = ?", (task_id, seq)
        ).fetchone()
        assert stored is not None
        return self._message_row(stored)

    def append_message(
        self,
        task_id: str,
        message_id: str,
        role: str,
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if role not in config.ROLES:
            raise StoreError(f"unknown role {role!r}")
        with self.conn:
            return self._insert_message(task_id, message_id, role, parts)

    @staticmethod
    def _message_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "message_id": row["message_id"],
            "role": row["role"],
            "parts": json.loads(row["parts_json"]),
            "seq": row["seq"],
            "created_at": row["created_at"],
        }

    def find_message(self, message_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE message_id = ? ORDER BY id LIMIT 1",
            (message_id,),
        ).fetchone()
        return self._message_row(row) if row else None

    def messages_for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [self._message_row(row) for row in rows]

    def last_message(self, task_id: str, role: str | None = None) -> dict[str, Any] | None:
        if role is None:
            row = self.conn.execute(
                "SELECT * FROM messages WHERE task_id = ? ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM messages WHERE task_id = ? AND role = ?"
                " ORDER BY seq DESC LIMIT 1",
                (task_id, role),
            ).fetchone()
        return self._message_row(row) if row else None

    @staticmethod
    def message_round(message: dict[str, Any]) -> int | None:
        for part in message["parts"]:
            data = part.get("data")
            if isinstance(data, dict) and "round" in data:
                value = data["round"]
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
        return None

    # -- turn lifecycle -----------------------------------------------------

    def begin_turn(
        self,
        task_id: str,
        message_id: str,
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """R14: store the incoming offer, set round_at_turn_start, go working.

        Does NOT change tasks.round (E-47).
        """

        with self.conn:
            task = self.get_task(task_id)
            if task is None:
                raise StoreError("task not found")
            state_machine.transition(task["state"], "working")
            self._insert_message(task_id, message_id, "user", parts)
            self.conn.execute(
                "UPDATE tasks SET state = 'working', round_at_turn_start = round,"
                " updated_at = ? WHERE id = ?",
                (_now_iso(), task_id),
            )
        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def resume_turn(self, task_id: str) -> dict[str, Any]:
        """R6c: re-enter working without inserting a message or touching round."""

        with self.conn:
            task = self.get_task(task_id)
            if task is None:
                raise StoreError("task not found")
            if task["round_at_turn_start"] is None:
                raise StoreError("resume requires round_at_turn_start")
            if task["state"] != "working":
                state_machine.transition(task["state"], "working")
                self.conn.execute(
                    "UPDATE tasks SET state = 'working', updated_at = ? WHERE id = ?",
                    (_now_iso(), task_id),
                )
        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def cas_commit(
        self,
        task_id: str,
        state: str,
        sent_kind: str,
        *,
        own_u: float | None = None,
        agent_message_id: str | None = None,
        agent_parts: list[dict[str, Any]] | None = None,
        agreement_terms: dict[str, int] | None = None,
    ) -> bool:
        """Compare-and-set the turn result (D-15, E-47).

        ``sent_kind`` is counter, accept, walk_away or failed. A counter
        advances tasks.round by exactly 2 (the incoming offer stored by R14
        plus the counter stored now); everything else advances by 0.

        The agent message (if any) is inserted in the SAME transaction as the
        CAS, so a cancel that wins the race leaves no orphan offer behind.
        """

        with self.conn:
            task = self.get_task(task_id)
            if task is None or task["state"] != "working":
                return False
            state_machine.transition("working", state)

            start = task["round_at_turn_start"]
            if start is None:
                start = task["round"]

            if sent_kind == "counter":
                new_round = int(start) + 2
                history = [float(v) for v in json.loads(task["own_utility_history"])]
                if own_u is not None:
                    history.append(float(own_u))
                history_json = json.dumps(history)
            elif sent_kind in {"accept", "walk_away", "failed"}:
                new_round = int(start)
                history_json = task["own_utility_history"]
            else:
                raise StoreError(f"unknown sent_kind {sent_kind!r}")

            cursor = self.conn.execute(
                "UPDATE tasks SET state = ?, round = ?, round_at_turn_start = NULL,"
                " own_utility_history = ?, updated_at = ?"
                " WHERE id = ? AND state = 'working'",
                (state, new_round, history_json, _now_iso(), task_id),
            )
            if cursor.rowcount == 1 and agent_parts is not None:
                self._insert_message(
                    task_id,
                    agent_message_id or str(uuid.uuid4()),
                    "agent",
                    agent_parts,
                )
            if cursor.rowcount == 1 and agreement_terms is not None:
                artifact = protocol.agreement_artifact(task_id, agreement_terms)
                self.conn.execute(
                    "INSERT INTO artifacts (task_id, artifact_id, name, parts_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        task_id,
                        artifact["artifactId"],
                        artifact["name"],
                        json.dumps(artifact["parts"]),
                        _now_iso(),
                    ),
                )
            return cursor.rowcount == 1

    def complete_with_accept(self, task_id: str, terms: dict[str, int]) -> dict[str, Any]:
        """R11: buyer accepted the seller's last offer. No LLM, no round change."""

        with self.conn:
            task = self.get_task(task_id)
            if task is None:
                raise StoreError("task not found")
            if task["state"] == "input-required":
                state_machine.transition("input-required", "working")
                state_machine.transition("working", "completed")
            elif task["state"] != "completed":
                state_machine.transition(task["state"], "completed")
            now = _now_iso()
            self.conn.execute(
                "UPDATE tasks SET state = 'completed', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            artifact = protocol.agreement_artifact(task_id, terms)
            self.conn.execute(
                "INSERT INTO artifacts (task_id, artifact_id, name, parts_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    artifact["artifactId"],
                    artifact["name"],
                    json.dumps(artifact["parts"]),
                    now,
                ),
            )
        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def cancel_task(self, task_id: str) -> tuple[dict[str, Any] | None, int | None]:
        """C3/C4/C5. Returns (task, error_code)."""

        with self.conn:
            task = self.get_task(task_id)
            if task is None:
                return None, config.A2A_TASK_NOT_FOUND
            if task["state"] == "canceled":
                return task, None
            if state_machine.is_terminal(task["state"]):
                return task, config.A2A_TASK_NOT_CANCELABLE
            state_machine.transition(task["state"], "canceled")
            self.conn.execute(
                "UPDATE tasks SET state = 'canceled', updated_at = ? WHERE id = ?",
                (_now_iso(), task_id),
            )
        updated = self.get_task(task_id)
        assert updated is not None
        return updated, None

    def startup_recovery(self) -> int:
        """T8: submitted/working -> input-required, round_at_turn_start = round."""

        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE state IN ('submitted','working')"
        ).fetchall()
        recovered = 0
        with self.conn:
            for row in rows:
                self.conn.execute(
                    "UPDATE tasks SET state = 'input-required',"
                    " round_at_turn_start = round, updated_at = ? WHERE id = ?",
                    (_now_iso(), row["id"]),
                )
                recovered += 1
        return recovered

    # -- views --------------------------------------------------------------

    def _artifact_rows(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [
            {
                "artifactId": row["artifact_id"],
                "name": row["name"],
                "parts": json.loads(row["parts_json"]),
            }
            for row in rows
        ]

    def task_view(
        self, task_id: str, history_length: int = config.MAX_HISTORY_RESPONSE
    ) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None

        limit = max(0, min(int(history_length), config.MAX_HISTORY_RESPONSE))
        messages = self.messages_for_task(task_id)
        history = messages[-limit:] if limit else []

        last_agent = self.last_message(task_id, "agent")
        status: dict[str, Any] = {
            "state": config.A2A_STATE_BY_INTERNAL[task["state"]],
            "timestamp": task["updated_at"],
        }
        if last_agent is not None:
            status["message"] = self._message_wire(last_agent)

        return {
            "id": task["id"],
            "contextId": task["context_id"],
            "status": status,
            "history": [self._message_wire(m) for m in history],
            "artifacts": self._artifact_rows(task_id),
        }

    @staticmethod
    def _message_wire(message: dict[str, Any]) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "messageId": message["message_id"],
            "role": config.A2A_ROLE_BY_INTERNAL[message["role"]],
            "parts": message["parts"],
        }
        if message["task_id"]:
            wire["taskId"] = message["task_id"]
        return wire

    def artifacts_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return self._artifact_rows(task_id)
