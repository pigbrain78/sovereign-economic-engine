from app.pocket_memory_adapter import PocketMemoryAdapter
from app.pocket_memory.normalize import content_hash
import sqlite3


def test_pocket_memory_adapter_deduplicates_and_builds_context(tmp_path):
    memory = PocketMemoryAdapter(tmp_path / 'memory.db')
    first = memory.remember('The local runtime is enabled.', memory_type='fact', actor='operator')
    duplicate = memory.remember('The local runtime is enabled.', memory_type='fact', actor='operator')
    assert first['deduplicated'] is False
    assert duplicate['deduplicated'] is True
    assert duplicate['memory_id'] == first['memory_id']
    context = memory.context('local runtime', top_k=5)
    assert context['context_hash']
    assert context['uncertainties'] or context['facts']
    assert memory.health()['ledger_verified'] is True


def test_pocket_memory_adapter_keeps_review_boundary_for_irreversible_retract(tmp_path):
    memory = PocketMemoryAdapter(tmp_path / 'memory.db')
    created = memory.remember('Keep this record.', memory_type='fact', actor='operator')
    result = memory.retract(created['memory_id'], 'operator requested removal', 'operator')
    assert result['requires_review'] is True


def test_pocket_memory_adapter_migrates_legacy_memory_schema(tmp_path):
    path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE memories (
            memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, memory_type TEXT,
            source_id TEXT, actor TEXT, created_at TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE, retracted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE memory_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL, memory_id TEXT NOT NULL, payload TEXT NOT NULL,
            previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
    ''')
    content = 'Legacy memory survives migration.'
    conn.execute(
        'INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ('legacy-1', content, 'fact', 'legacy-source', 'operator', '2026-01-01T00:00:00+00:00', content_hash(content), 0),
    )
    conn.commit(); conn.close()

    memory = PocketMemoryAdapter(path)
    result = memory.search('legacy memory')
    assert result and result[0]['memory_id'] == 'legacy-1'
    assert memory.health()['ledger_verified'] is True
