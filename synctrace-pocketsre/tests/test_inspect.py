import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from core.ledger import Ledger
from synctrace.inspect import InspectRejected, SQLiteInspector


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "app.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL);"
        "INSERT INTO notes(body) VALUES ('one'), ('two'), ('three');"
        "CREATE VIEW v AS SELECT body FROM notes;"
    )
    con.commit()
    con.close()
    return path


def test_lists_tables_and_views(db_path):
    insp = SQLiteInspector(db_path)
    assert insp.tables() == [{"name": "notes", "type": "table"}, {"name": "v", "type": "view"}]
    insp.close()


def test_columns_and_count(db_path):
    insp = SQLiteInspector(db_path)
    cols = insp.columns("notes")
    assert cols[0] == {"name": "id", "type": "INTEGER", "not_null": False, "primary_key": True}
    assert insp.row_count("notes") == 3
    assert len(insp.sample("notes", limit=2)) == 2
    insp.close()


def test_unknown_table_rejected(db_path):
    insp = SQLiteInspector(db_path)
    with pytest.raises(InspectRejected):
        insp.row_count("notes; DROP TABLE notes")
    insp.close()


def test_writes_are_refused(db_path):
    insp = SQLiteInspector(db_path)
    for sql in ["DELETE FROM notes", "DROP TABLE notes", "PRAGMA writable_schema=1",
                "ATTACH 'x.db' AS x", "SELECT 1; DELETE FROM notes"]:
        with pytest.raises(InspectRejected):
            insp.query(sql)
    insp.close()
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 3


def test_select_query_works(db_path):
    insp = SQLiteInspector(db_path)
    assert insp.query("SELECT body FROM notes WHERE id = 2") == [{"body": "two"}]
    insp.close()


def test_inspections_and_refusals_are_logged(db_path):
    ledger = Ledger(":memory:")
    insp = SQLiteInspector(db_path, ledger=ledger)
    insp.row_count("notes")
    insp.query("SELECT body FROM notes WHERE id = 1")
    with pytest.raises(InspectRejected):
        insp.query("DELETE FROM notes")
    insp.close()
    kinds = [row[0] for row in ledger.db.execute("SELECT kind FROM events ORDER BY seq")]
    assert kinds == ["INSPECT_OPENED", "INSPECT_COUNT", "INSPECT_QUERY",
                     "INSPECT_REFUSED", "INSPECT_CLOSED"]
    assert ledger.verify()


def test_missing_database_rejected(tmp_path):
    with pytest.raises(InspectRejected):
        SQLiteInspector(tmp_path / "nope.db")
