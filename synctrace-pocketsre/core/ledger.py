"""Hash-chained, append-only event log shared by SyncTrace and PocketSRE.

Each row stores a seal over (seq, kind, payload, prev_hash, ts). With a key the
seal is HMAC-SHA256, so someone who can edit the database file cannot re-seal a
changed chain without the key. Without a key it is plain SHA-256: integrity only.

verify() also catches removed middle rows (seq gap), and tail truncation when
given expected_count or expected_head from a copy kept outside the database.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from typing import Any

GENESIS = "0" * 64


class Ledger:
    def __init__(self, path: str = ":memory:", key: bytes | None = None) -> None:
        self.key = key
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "seq INTEGER PRIMARY KEY, kind TEXT NOT NULL,"
            "payload TEXT NOT NULL, prev_hash TEXT NOT NULL,"
            "hash TEXT NOT NULL UNIQUE, ts REAL NOT NULL)"
        )
        self.db.commit()

    def _seal(self, seq: int, kind: str, payload: str, prev_hash: str, ts: float) -> str:
        body = json.dumps({"seq": seq, "kind": kind, "payload": payload,
                           "prev": prev_hash, "ts": ts},
                          sort_keys=True, separators=(",", ":")).encode()
        if self.key is not None:
            return hmac.new(self.key, body, hashlib.sha256).hexdigest()
        return hashlib.sha256(body).hexdigest()

    def _last(self) -> tuple[int, str]:
        row = self.db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    def append(self, kind: str, payload: dict[str, Any]) -> str:
        last_seq, prev = self._last()
        seq = last_seq + 1
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        ts = time.time()
        digest = self._seal(seq, kind, text, prev, ts)
        self.db.execute(
            "INSERT INTO events(seq, kind, payload, prev_hash, hash, ts) VALUES (?,?,?,?,?,?)",
            (seq, kind, text, prev, digest, ts))
        self.db.commit()
        return digest

    def head(self) -> str:
        return self._last()[1]

    def count(self) -> int:
        return self._last()[0]

    def verify(self, *, expected_count: int | None = None, expected_head: str | None = None) -> bool:
        prev = GENESIS
        expected_seq = 1
        for seq, kind, payload, prev_hash, digest, ts in self.db.execute(
                "SELECT seq, kind, payload, prev_hash, hash, ts FROM events ORDER BY seq"):
            if seq != expected_seq:  # a row was removed from the middle
                return False
            if prev_hash != prev:
                return False
            if not hmac.compare_digest(self._seal(seq, kind, payload, prev_hash, ts), digest):
                return False
            prev = digest
            expected_seq += 1
        if expected_count is not None and expected_seq - 1 != expected_count:
            return False  # tail truncation (or extra rows)
        if expected_head is not None and prev != expected_head:
            return False
        return True
