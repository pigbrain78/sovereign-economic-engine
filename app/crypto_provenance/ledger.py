"""Durable append-only SQLite event ledger with a verifiable hash chain."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import threading
from pathlib import Path
from typing import Any
import uuid

from .canonical import canonical_json, digest

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class LedgerEvent:
    sequence: int
    event_id: str
    event_type: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class LedgerIntegrityError(RuntimeError):
    """Raised when persisted ledger content no longer verifies."""


class SQLiteLedger:
    """Single-writer, append-only ledger suitable for local persistence."""

    def __init__(self, path: str | Path = "data/reflex-ledger.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS ledger_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            )"""
        )
        self._conn.commit()

    @property
    def head_hash(self) -> str:
        row = self._conn.execute(
            "SELECT event_hash FROM ledger_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    def __len__(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0])

    @staticmethod
    def _event_hash(event_id: str, event_type: str, payload: dict[str, Any], previous_hash: str) -> str:
        return digest(
            {
                "event_id": event_id,
                "event_type": event_type,
                "payload": payload,
                "previous_hash": previous_hash,
            },
            "reflex.ledger.event",
        )

    def append(self, event_type: str, payload: dict[str, Any], event_id: str | None = None) -> LedgerEvent:
        if not event_type:
            raise ValueError("event_type is required")
        canonical_json(payload)  # validate before opening the write transaction
        with self._lock, self._conn:
            previous_hash = self.head_hash
            eid = event_id or f"evt_{uuid.uuid4().hex}"
            event_hash = self._event_hash(eid, event_type, payload, previous_hash)
            cursor = self._conn.execute(
                "INSERT INTO ledger_events (event_id,event_type,payload,previous_hash,event_hash) VALUES (?,?,?,?,?)",
                (eid, event_type, canonical_json(payload), previous_hash, event_hash),
            )
            return LedgerEvent(cursor.lastrowid, eid, event_type, payload, previous_hash, event_hash)

    def events(self) -> list[LedgerEvent]:
        rows = self._conn.execute("SELECT * FROM ledger_events ORDER BY sequence").fetchall()
        import json
        return [
            LedgerEvent(row["sequence"], row["event_id"], row["event_type"], json.loads(row["payload"]), row["previous_hash"], row["event_hash"])
            for row in rows
        ]

    def verify(self) -> None:
        previous = GENESIS_HASH
        for event in self.events():
            if event.previous_hash != previous:
                raise LedgerIntegrityError(f"chain break at {event.event_id}")
            expected = self._event_hash(event.event_id, event.event_type, event.payload, event.previous_hash)
            if expected != event.event_hash:
                raise LedgerIntegrityError(f"hash mismatch at {event.event_id}")
            previous = event.event_hash

    def close(self) -> None:
        self._conn.close()
