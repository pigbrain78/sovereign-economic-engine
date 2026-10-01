from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MemoryAPI:
    """Durable, deduplicating memory with append-only provenance events."""

    def __init__(self, db_path: str | Path = 'sovereign-memory.db') -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                memory_type TEXT,
                source_id TEXT,
                actor TEXT,
                created_at TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                retracted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memory_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            ''')
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(' '.join(content.split()).casefold().encode()).hexdigest()

    @staticmethod
    def _canonical(value: Any) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()

    def _append_event(self, db: sqlite3.Connection, event_type: str, memory_id: str, payload: dict[str, Any]) -> None:
        previous = db.execute('SELECT event_hash FROM memory_events ORDER BY sequence DESC LIMIT 1').fetchone()
        previous_hash = previous['event_hash'] if previous else '0' * 64
        created_at = self._now()
        body = {'event_type': event_type, 'memory_id': memory_id, 'payload': payload, 'previous_hash': previous_hash, 'created_at': created_at}
        event_hash = hashlib.sha256(self._canonical(body)).hexdigest()
        event_id = hashlib.sha256(event_hash.encode()).hexdigest()[:32]
        db.execute('INSERT INTO memory_events(event_id,event_type,memory_id,payload,previous_hash,event_hash,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)', (event_id, event_type, memory_id, json.dumps(payload, sort_keys=True), previous_hash, event_hash, created_at))

    def remember(self, content: str, *, memory_type: str | None = None, source_id: str | None = None, actor: str | None = None) -> dict[str, Any]:
        if not content or not content.strip():
            raise ValueError('content must not be empty')
        content = content.strip()
        content_hash = self._hash(content)
        with closing(self._connect()) as db:
            existing = db.execute('SELECT * FROM memories WHERE content_hash = ?', (content_hash,)).fetchone()
            if existing:
                return {**dict(existing), 'deduplicated': True}
            created_at = self._now()
            memory_id = hashlib.sha256(f'{content_hash}:{created_at}'.encode()).hexdigest()[:32]
            db.execute('INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, 0)', (memory_id, content, memory_type, source_id, actor, created_at, content_hash))
            self._append_event(db, 'MEMORY_CAPTURED', memory_id, {'content_hash': content_hash, 'source_id': source_id, 'actor': actor})
            db.commit()
            return {**dict(db.execute('SELECT * FROM memories WHERE memory_id = ?', (memory_id,)).fetchone()), 'deduplicated': False}

    def get(self, memory_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM memories WHERE memory_id = ?', (memory_id,)).fetchone()
            return dict(row) if row else None

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        if top_k < 1:
            raise ValueError('top_k must be positive')
        terms = [term for term in query.casefold().split() if term]
        if not terms:
            return []
        with closing(self._connect()) as db:
            rows = db.execute('SELECT * FROM memories WHERE retracted = 0').fetchall()
            ranked = [(sum(term in row['content'].casefold() for term in terms), row) for row in rows]
            return [dict(row) for score, row in sorted(ranked, key=lambda item: (item[0], item[1]['created_at']), reverse=True)[:top_k] if score]

    def retract(self, memory_id: str, reason: str = '', actor: str = 'system') -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute('SELECT retracted FROM memories WHERE memory_id = ?', (memory_id,)).fetchone()
            if not row:
                raise KeyError(memory_id)
            if row['retracted']:
                return {'memory_id': memory_id, 'retracted': True, 'idempotent_replay': True}
            db.execute('UPDATE memories SET retracted = 1 WHERE memory_id = ?', (memory_id,))
            self._append_event(db, 'MEMORY_RETRACTED', memory_id, {'reason': reason, 'actor': actor})
            db.commit()
            return {'memory_id': memory_id, 'retracted': True, 'reason': reason, 'actor': actor}

    def explain(self, memory_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            memory = db.execute('SELECT * FROM memories WHERE memory_id = ?', (memory_id,)).fetchone()
            events = [dict(row) for row in db.execute('SELECT * FROM memory_events WHERE memory_id = ? ORDER BY sequence', (memory_id,)).fetchall()]
            return {'memory': dict(memory) if memory else None, 'provenance': events, 'history': events}

    def verify_ledger(self) -> bool:
        with closing(self._connect()) as db:
            previous = '0' * 64
            for row in db.execute('SELECT * FROM memory_events ORDER BY sequence').fetchall():
                if row['previous_hash'] != previous:
                    return False
                body = {'event_type': row['event_type'], 'memory_id': row['memory_id'], 'payload': json.loads(row['payload']), 'previous_hash': row['previous_hash'], 'created_at': row['created_at']}
                if hashlib.sha256(self._canonical(body)).hexdigest() != row['event_hash']:
                    return False
                previous = row['event_hash']
            return True

    def health(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            count = db.execute('SELECT COUNT(*) FROM memories WHERE retracted = 0').fetchone()[0]
        return {'status': 'ok', 'active_memories': count, 'ledger_verified': self.verify_ledger()}
