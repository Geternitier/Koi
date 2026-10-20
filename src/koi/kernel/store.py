"""SQLite 存储：键值状态与事件日志。"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from koi.kernel.bus import Event
from koi.protocol import AuditRecord

M = TypeVar("M", bound=BaseModel)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    type TEXT NOT NULL,
    priority INTEGER NOT NULL,
    source TEXT NOT NULL,
    trust TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events(time);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    plugin TEXT NOT NULL,
    capability TEXT NOT NULL,
    args TEXT NOT NULL,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def get_json(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_json(self, key: str, value: Any) -> None:
        self._put(key, json.dumps(value, ensure_ascii=False))

    def get_model(self, key: str, model: type[M]) -> M | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return model.model_validate_json(row[0]) if row else None

    def put_model(self, key: str, value: BaseModel) -> None:
        self._put(key, value.model_dump_json())

    def _put(self, key: str, raw: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, raw, datetime.now().astimezone().isoformat()),
            )

    def append_event(self, event: Event) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.time.isoformat(),
                    event.type,
                    int(event.priority),
                    event.source,
                    event.trust.value,
                    json.dumps(event.payload, ensure_ascii=False),
                ),
            )

    def append_audit(self, record: AuditRecord) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO audit(time, plugin, capability, args, ok, summary) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.time.isoformat(),
                    record.plugin,
                    record.capability,
                    record.args,
                    int(record.ok),
                    record.summary,
                ),
            )

    def recent_audit(self, limit: int = 50) -> list[AuditRecord]:
        rows = self._conn.execute(
            "SELECT time, plugin, capability, args, ok, summary FROM audit "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            AuditRecord(
                time=datetime.fromisoformat(r[0]),
                plugin=r[1],
                capability=r[2],
                args=r[3],
                ok=bool(r[4]),
                summary=r[5],
            )
            for r in rows
        ]

    def events_between(self, start: datetime, end: datetime) -> list[Event]:
        rows = self._conn.execute(
            "SELECT id, time, type, priority, source, trust, payload FROM events "
            "WHERE time >= ? AND time < ? ORDER BY time",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [
            Event(
                id=r[0],
                time=datetime.fromisoformat(r[1]),
                type=r[2],
                priority=r[3],
                source=r[4],
                trust=r[5],
                payload=json.loads(r[6]),
            )
            for r in rows
        ]
