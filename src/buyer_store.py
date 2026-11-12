"""Buyer-side resume store (section 7.1, E-33).

Persists the exact bytes of the last message the buyer sent so that
``--resume`` can resend it byte-identically (same messageId AND same
body), which makes R6b/R6c fire and prevents R9 from tripping on a
re-derived message with a different round field.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from src import clock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_state (
  url TEXT NOT NULL,
  task_id TEXT NOT NULL,
  last_message_id TEXT,
  last_message_json TEXT,
  last_round INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (url, task_id)
)
"""


class BuyerStore:
    def __init__(self, path: str, *, journal_mode: str = "WAL") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA journal_mode={journal_mode}")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(_SCHEMA)

    def record(
        self,
        url: str,
        task_id: str,
        last_message: dict[str, Any],
    ) -> None:
        body = json.dumps(last_message, separators=(",", ":"), sort_keys=True)
        now = clock.now().isoformat()
        self.conn.execute(
            """
            INSERT INTO resume_state (url, task_id, last_message_id, last_message_json, last_round, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url, task_id) DO UPDATE SET
              last_message_id=excluded.last_message_id,
              last_message_json=excluded.last_message_json,
              last_round=excluded.last_round,
              updated_at=excluded.updated_at
            """,
            (
                url,
                task_id,
                last_message.get("messageId"),
                body,
                _round_of(last_message),
                now,
            ),
        )
        self.conn.commit()

    def latest(self, url: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM resume_state WHERE url = ? ORDER BY updated_at DESC LIMIT 1",
            (url,),
        ).fetchone()
        return dict(row) if row else None

    def get(self, url: str, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM resume_state WHERE url = ? AND task_id = ?", (url, task_id)
        ).fetchone()
        return dict(row) if row else None

    def clear(self, url: str, task_id: str) -> None:
        self.conn.execute(
            "DELETE FROM resume_state WHERE url = ? AND task_id = ?", (url, task_id)
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _round_of(message: dict[str, Any]) -> int | None:
    for part in message.get("parts", ()):
        data = part.get("data") if isinstance(part, dict) else None
        if isinstance(data, dict):
            value = data.get("round")
            return value if isinstance(value, int) else None
    return None
