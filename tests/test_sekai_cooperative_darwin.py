import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ['SOVEREIGN_DB'] = '/tmp/sovereign-sekai-api.db'
os.environ['SOVEREIGN_MEMORY_DB'] = '/tmp/sovereign-sekai-memory.db'
os.environ['SOVEREIGN_FACTORY_LEDGER'] = '/tmp/sovereign-sekai-factory-ledger.jsonl'
for path in (
    '/tmp/sovereign-sekai-api.db',
    '/tmp/sovereign-sekai-memory.db',
    '/tmp/sovereign-sekai-factory-ledger.jsonl',
):
    Path(path).unlink(missing_ok=True)

from app.main import app  # noqa: E402

client = TestClient(app)


def test_sekai_cooperative_darwin_and_settlement_flow():
    wallet = client.post('/wallets', json={'capital': '20.00', 'minimum_liquidity': '2.00'}).json()
    coop = client.post('/nexus/sekai/cooperatives', json={'name': 'Sekai Cooperative', 'treasury_wallet_id': wallet['id']})
    assert coop.status_code == 201
    cooperative_id = coop.json()['cooperative_id']

    model_a = client.post('/models', json={
        'name': 'Research Swift',
        'task_types': ['research'],
        'quality': 0.90,
        'reliability': 0.95,
        'cost_per_task': '0.08',
        'latency_ms': 500,
    }).json()
    model_b = client.post('/models', json={
        'name': 'Research Deep',
        'task_types': ['research'],
        'quality': 0.96,
        'reliability': 0.94,
        'cost_per_task': '0.12',
        'latency_ms': 900,
    }).json()

    agent_a = client.post(f'/nexus/sekai/cooperatives/{cooperative_id}/agents', json={
        'name': 'Orion',
        'role': 'researcher',
        'model_id': model_a['id'],
        'skills': ['search', 'synthesis'],
        'reputation': 88,
    }).json()
    agent_b = client.post(f'/nexus/sekai/cooperatives/{cooperative_id}/agents', json={
        'name': 'Luna',
        'role': 'researcher',
        'model_id': model_b['id'],
        'skills': ['analysis', 'verification'],
        'reputation': 82,
    }).json()

    job = client.post(f'/nexus/sekai/cooperatives/{cooperative_id}/jobs', json={
        'title': 'Research market landscape',
        'task_type': 'research',
        'budget': '0.30',
        'minimum_quality': 0.80,
    })
    assert job.status_code == 201
    job_id = job.json()['job_id']

    bid_a = client.post(f'/nexus/sekai/jobs/{job_id}/bids', json={
        'agent_id': agent_a['agent_id'],
        'model_id': model_a['id'],
        'expected_cost': '0.08',
        'expected_quality': 0.85,
        'confidence': 0.88,
        'latency_ms': 500,
    })
    assert bid_a.status_code == 201

    bid_b = client.post(f'/nexus/sekai/jobs/{job_id}/bids', json={
        'agent_id': agent_b['agent_id'],
        'model_id': model_b['id'],
        'expected_cost': '0.12',
        'expected_quality': 0.92,
        'confidence': 0.85,
        'latency_ms': 900,
    })
    assert bid_b.status_code == 201

    selected = client.post(f'/nexus/sekai/jobs/{job_id}/select')
    assert selected.status_code == 200
    assert selected.json()['job']['status'] == 'selected'

    settled = client.post(f'/nexus/sekai/jobs/{job_id}/settle', json={
        'actor': 'operator',
        'execution_id': 'sekai-exec-001',
        'cpu_seconds': 0.1,
        'peak_memory_bytes': 1024,
        'io_read_bytes': 256,
        'io_write_bytes': 128,
        'token_count': 100000,
        'wall_seconds': 0.1,
        'outcome': 'verified_success',
        'evidence': {'mode': 'sekai-test'},
    })
    assert settled.status_code == 201
    assert settled.json()['job']['status'] == 'settled'
    assert settled.json()['execution']['outcome'] == 'verified_success'

    scoreboard = client.get(f'/nexus/sekai/cooperatives/{cooperative_id}/scoreboard')
    assert scoreboard.status_code == 200
    assert scoreboard.json()['metrics']['settled_jobs'] == 1

    events = client.get('/nexus/events', params={'domain': 'sekai'}).json()
    assert any(event['event_type'] == 'SEKAI_DARWIN_WINNER_SELECTED' for event in events)
    assert any(event['event_type'] == 'SEKAI_JOB_SETTLED' for event in events)
