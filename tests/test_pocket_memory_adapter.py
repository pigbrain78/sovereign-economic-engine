from app.pocket_memory_adapter import PocketMemoryAdapter


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
