"""Read-only SQLite inspection for a local-first app's on-device database.

The database is opened with mode=ro, so SQLite itself refuses writes. Queries
also pass through an authorizer that allows only SELECT-style reads. Table
names are checked against sqlite_master before they are used in SQL. When a
Ledger is given, every inspection and every refusal is recorded in it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core.ledger import Ledger

MAX_ROWS = 200

_ALLOWED_ACTIONS = {
    action for action in (
        getattr(sqlite3, "SQLITE_SELECT", None),
        getattr(sqlite3, "SQLITE_READ", None),
        getattr(sqlite3, "SQLITE_FUNCTION", None),
        getattr(sqlite3, "SQLITE_RECURSIVE", None),
    ) if action is not None
}


class InspectRejected(ValueError):
    pass


def _authorizer(action, arg1, arg2, dbname, source):
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    # Column metadata only: PRAGMA table_info is read-only and needed by columns().
    if action == getattr(sqlite3, "SQLITE_PRAGMA", None) and arg1 == "table_info":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class SQLiteInspector:
    def __init__(self, db_path: str | Path, ledger: Ledger | None = None) -> None:
        path = Path(db_path)
        self.ledger = ledger
        self.db_name = path.name
        if not path.is_file():
            self._log("INSPECT_REFUSED", {"db": self.db_name, "reason": "missing database"})
            raise InspectRejected(f"database not found: {path}")
        uri = f"file:{quote(str(path.resolve()))}?mode=ro"
        self.db = sqlite3.connect(uri, uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.set_authorizer(_authorizer)
        self._log("INSPECT_OPENED", {"db": self.db_name})

    def _log(self, kind: str, payload: dict[str, Any]) -> None:
        if self.ledger is not None:
            self.ledger.append(kind, payload)

    def close(self) -> None:
        self._log("INSPECT_CLOSED", {"db": self.db_name})
        self.db.close()

    def tables(self) -> list[dict[str, str]]:
        rows = self.db.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [{"name": r["name"], "type": r["type"]} for r in rows]

    def _known_table(self, name: str) -> str:
        known = {t["name"] for t in self.tables()}
        if name not in known:
            self._log("INSPECT_REFUSED", {"db": self.db_name, "reason": "unknown table", "table": name})
            raise InspectRejected(f"unknown table: {name}")
        return '"' + name.replace('"', '""') + '"'

    def columns(self, table: str) -> list[dict[str, Any]]:
        quoted = self._known_table(table)
        self._log("INSPECT_COLUMNS", {"db": self.db_name, "table": table})
        return [
            {"name": r["name"], "type": r["type"], "not_null": bool(r["notnull"]),
             "primary_key": bool(r["pk"])}
            for r in self.db.execute(f"PRAGMA table_info({quoted})").fetchall()
        ]

    def row_count(self, table: str) -> int:
        quoted = self._known_table(table)
        self._log("INSPECT_COUNT", {"db": self.db_name, "table": table})
        return self.db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]

    def sample(self, table: str, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= MAX_ROWS:
            raise InspectRejected(f"limit must be between 1 and {MAX_ROWS}")
        quoted = self._known_table(table)
        self._log("INSPECT_SAMPLE", {"db": self.db_name, "table": table, "limit": limit})
        return [dict(r) for r in self.db.execute(
            f"SELECT * FROM {quoted} LIMIT ?", (limit,)).fetchall()]

    def query(self, sql: str, limit: int = 100) -> list[dict[str, Any]]:
        """Run one read-only statement. Writes, DDL, PRAGMA and ATTACH are denied."""
        statement = sql.strip().rstrip(";").strip()
        if not statement or ";" in statement:
            self._refuse(sql, "not exactly one statement")
        if statement.split(None, 1)[0].upper() not in {"SELECT", "WITH"}:
            self._refuse(sql, "not a SELECT or WITH query")
        if not 1 <= limit <= MAX_ROWS:
            self._refuse(sql, "limit out of range")
        try:
            cursor = self.db.execute(statement)
        except sqlite3.DatabaseError as exc:
            self._refuse(sql, f"engine refused: {exc}")
        self._log("INSPECT_QUERY", {"db": self.db_name, "sql": sql.strip(), "limit": limit})
        return [dict(r) for r in cursor.fetchmany(limit)]

    def _refuse(self, sql: str, reason: str) -> None:
        self._log("INSPECT_REFUSED", {"db": self.db_name, "sql": sql.strip(), "reason": reason})
        raise InspectRejected(f"query refused: {reason}")
