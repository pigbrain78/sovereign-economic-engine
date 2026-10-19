"""schema.py -- SQLite schema contract.

Single SQLite file holds the authoritative append-only ledger plus derived
projection tables. The schema contract is shared with a Postgres backend so a
SQLite -> Postgres migration is a backend swap, not a redesign. Derived
projections (vector index, graph, cache, snapshots) are never authoritative;
the ledger is.

Tables (kept minimal):
  ledger_events          -- authoritative append-only hash-chained event stream
  memories               -- current projection of active memory records
  memory_versions        -- immutable snapshot of a memory's state at each change
  memory_sources         -- source records (document, conversation, agent, ...)
  memory_relationships   -- graph edges (SUPPORTS, CONTRADICTS, SUPERSEDES, ...)
  memory_contradictions  -- explicit unresolved / resolved contradiction pairs
  memory_consolidations  -- reproducible consolidation operations
  review_queue           -- human review items (governance gate)
  memory_access_log      -- audit trail of operations
  memory_snapshots       -- deterministic snapshots
"""

from __future__ import annotations

import sqlite3

from . import canonical

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_events (
    event_id            TEXT PRIMARY KEY,
    timestamp           TEXT NOT NULL,          -- ISO-8601 UTC
    event_type          TEXT NOT NULL,
    payload             TEXT NOT NULL,          -- canonical JSON
    previous_event_hash TEXT,
    event_hash          TEXT NOT NULL UNIQUE,
    algorithm_version   TEXT NOT NULL DEFAULT '1'
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id           TEXT PRIMARY KEY,
    memory_type         TEXT NOT NULL,
    content             TEXT NOT NULL,
    normalized_content  TEXT,
    status              TEXT NOT NULL,          -- ACTIVE / SUPERSEDED / ...
    confidence          REAL NOT NULL,          -- current_confidence
    importance          REAL NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    event_id            TEXT NOT NULL,          -- committing ledger event
    supersedes          TEXT,
    superseded_by       TEXT,
    source_id           TEXT,
    content_hash        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_versions (
    version_id          TEXT PRIMARY KEY,
    memory_id           TEXT NOT NULL,
    status              TEXT NOT NULL,
    content             TEXT NOT NULL,
    confidence          REAL NOT NULL,
    importance          REAL NOT NULL,
    captured_at         TEXT NOT NULL,
    event_id            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_sources (
    source_id           TEXT PRIMARY KEY,
    source_type         TEXT NOT NULL,          -- document / conversation / url / agent / ...
    external_ref        TEXT,                   -- e.g. path, URL, conversation id
    author              TEXT,
    observed_at         TEXT,
    raw_hash            TEXT,
    extra               TEXT                    -- canonical JSON of optional fields
);

CREATE TABLE IF NOT EXISTS memory_relationships (
    rel_id              TEXT PRIMARY KEY,
    from_memory         TEXT NOT NULL,
    to_memory           TEXT NOT NULL,
    rel_type            TEXT NOT NULL,          -- SUPPORTS / CONTRADICTS / SUPERSEDES / DERIVED_FROM / ...
    created_at          TEXT NOT NULL,
    event_id            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_contradictions (
    contradiction_id    TEXT PRIMARY KEY,
    memory_a            TEXT NOT NULL,
    memory_b            TEXT NOT NULL,
    status              TEXT NOT NULL,          -- UNRESOLVED / RESOLVED
    resolution          TEXT,                   -- SUPERSEDED / CONTRADICTED / BOTH_ACTIVE / RETRACTED
    decided_memory      TEXT,                   -- which side kept active (if any)
    detected_at         TEXT NOT NULL,
    resolved_at         TEXT,
    event_id            TEXT NOT NULL,
    detail              TEXT
);

CREATE TABLE IF NOT EXISTS memory_consolidations (
    consolidation_id    TEXT PRIMARY KEY,
    input_memories      TEXT NOT NULL,          -- canonical JSON list
    output_memory       TEXT NOT NULL,
    algorithm_version   TEXT NOT NULL,
    agent               TEXT,
    created_at          TEXT NOT NULL,
    confidence          REAL,
    reason              TEXT,
    event_id            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    item_id             TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,          -- contradiction / merge / low_confidence / sensitive / ...
    ref_json            TEXT NOT NULL,          -- canonical JSON describing the item
    status              TEXT NOT NULL,          -- PENDING / APPROVED / REJECTED / MERGED / ...
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    decided_by          TEXT,
    decision_event_id   TEXT
);

CREATE TABLE IF NOT EXISTS memory_access_log (
    log_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor               TEXT,
    operation           TEXT NOT NULL,
    target              TEXT,
    timestamp           TEXT NOT NULL,
    detail              TEXT
);

CREATE TABLE IF NOT EXISTS memory_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    ledger_position     TEXT,                   -- last event_id covered
    memory_count        INTEGER NOT NULL,
    graph_state_hash    TEXT,
    index_state_hash    TEXT,
    memory_state_hash   TEXT,
    created_at          TEXT NOT NULL,
    algorithm_versions  TEXT
);
"""


def _migrate_legacy_memories(conn: sqlite3.Connection) -> None:
    """Upgrade the pre-MemoryBrain memories table without discarding records."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    if not columns or "status" in columns:
        return
    if "content" not in columns or "memory_id" not in columns:
        raise RuntimeError("unsupported memories table; refusing destructive migration")

    conn.execute("ALTER TABLE memories RENAME TO memories_legacy")
    conn.executescript(SCHEMA_SQL)
    legacy_rows = conn.execute("SELECT * FROM memories_legacy ORDER BY rowid").fetchall()
    for row in legacy_rows:
        source_id = row[3]
        if source_id and conn.execute("SELECT 1 FROM memory_sources WHERE source_id=?", (source_id,)).fetchone() is None:
            conn.execute(
                "INSERT INTO memory_sources (source_id, source_type, external_ref) VALUES (?, ?, ?)",
                (source_id, "legacy", source_id),
            )
        event_id = f"legacy-memory-{row[0]}"
        payload = {
            "content": row[1],
            "memory_type": row[2] or "OBSERVATION",
            "source_id": source_id,
            "content_hash": row[6],
            "legacy": True,
        }
        previous = conn.execute("SELECT event_hash FROM ledger_events ORDER BY rowid DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else None
        timestamp = row[5]
        event_body = {
            "event_id": event_id,
            "timestamp": timestamp,
            "event_type": "MEMORY_CREATED",
            "payload": payload,
            "previous_event_hash": previous_hash,
            "algorithm_version": "1",
        }
        event_hash = canonical.digest(event_body, canonical.LEDGER_EVENT)
        conn.execute(
            "INSERT INTO ledger_events (event_id,timestamp,event_type,payload,previous_event_hash,event_hash,algorithm_version) VALUES (?,?,?,?,?,?,?)",
            (event_id, timestamp, "MEMORY_CREATED", canonical.canonical_json(payload), previous_hash, event_hash, "1"),
        )
        normalized = row[1].strip().lower()
        conn.execute(
            "INSERT INTO memories (memory_id,memory_type,content,normalized_content,status,confidence,importance,created_at,updated_at,event_id,source_id,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (row[0], row[2] or "OBSERVATION", row[1], normalized, "RETRACTED" if row[7] else "ACTIVE", 0.5, 0.4, timestamp, timestamp, event_id, source_id, row[6]),
        )
    conn.execute("DROP TABLE memories_legacy")


def load_schema(conn) -> None:
    """Apply the schema and safely upgrade the pre-MemoryBrain table."""
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _migrate_legacy_memories(conn)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
