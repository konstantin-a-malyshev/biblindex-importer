"""SQLite-backed cache of API responses.

Every successful GET is stored by its canonical URL. Re-running an interrupted
export replays cached responses instead of spending rate-limited requests again,
and later exports of other books reuse already-resolved authors and works.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class ResponseCache:
    def __init__(self, path: str | Path, ttl_seconds: float | None = None) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._db = sqlite3.connect(str(self.path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            " key TEXT PRIMARY KEY,"
            " body TEXT NOT NULL,"
            " fetched_at REAL NOT NULL)"
        )
        self._db.commit()

    def get(self, key: str) -> Any | None:
        row = self._db.execute(
            "SELECT body, fetched_at FROM responses WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        body, fetched_at = row
        if self.ttl_seconds is not None and time.time() - fetched_at > self.ttl_seconds:
            return None
        return json.loads(body)

    def set(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO responses (key, body, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        self._db.commit()

    def clear(self) -> None:
        self._db.execute("DELETE FROM responses")
        self._db.commit()

    def close(self) -> None:
        self._db.close()
