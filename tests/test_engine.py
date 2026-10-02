import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DB = Path('/tmp/sovereign-economic-engine-test.db')
if DB.exists():
    DB.unlink()
os.environ['SOVEREIGN_DB'] = str(DB)

from app.main import app  # noqa: E402

client = TestClient(app)


def test_end_to_end_wallet_route_and_settlement():
    wallet = client.post('/wallets', json={'capital': '10.00', 'minimum_liquidity': '2.00'}).json()
    model = client.post('/models', json={
        'id': 'cheap-reliable', 'name': 'Cheap Reliable', 'task_types': ['research'],
        'quality': 0.92, 'reliability': 0.98, 'cost_per_task': '0.50', 'latency_ms': 900,
    }).json()
    mission = client.post('/missions', json={
        'wallet_id': wallet['id'], 'description': 'Research target', 'max_cost': '1.00',
        'minimum_quality': 0.85, 'task_type': 'research',
    }).json()
    decision = client.post(f"/missions/{mission['id']}/decide").json()
    assert decision['status'] == 'APPROVED'
    assert decision['selected_model'] == model['id']

    execution = client.post('/executions', json={
        'mission_id': mission['id'], 'model_id': model['id'], 'execution_id': 'exec-001',
        'cpu_seconds': 0, 'peak_memory_bytes': 0, 'io_read_bytes': 0, 'io_write_bytes': 0,
        'token_count': 420000, 'wall_seconds': 0,
        'outcome': 'verified_success', 'evidence': {'validator': 'deterministic-demo'},
    })
    assert execution.status_code == 201
    assert execution.json()['wallet']['available'] == '7.580000'
    assert execution.json()['wallet']['spent'] == '0.420000'

    replay = client.post('/executions', json={
        'mission_id': mission['id'], 'model_id': model['id'], 'execution_id': 'exec-001',
        'cpu_seconds': 999, 'peak_memory_bytes': 999, 'io_read_bytes': 999, 'io_write_bytes': 999,
        'token_count': 999, 'outcome': 'failed',
    })
    assert replay.status_code == 201
    assert replay.json()['idempotent_replay'] is True
    assert replay.json()['wallet']['spent'] == '0.420000'

    ledger = client.get(f"/ledger/{wallet['id']}").json()
    assert [event['event'] for event in ledger] == ['deposit', 'allocate_development', 'allocate_safety', 'allocate_owner', 'reserve', 'outcome', 'release']


def test_route_holds_when_no_model_meets_quality_and_cost():
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    client.post('/models', json={
        'id': 'expensive-model', 'name': 'Expensive', 'task_types': ['general'],
        'quality': 0.99, 'reliability': 0.99, 'cost_per_task': '4.00', 'latency_ms': 1000,
    })
    mission = client.post('/missions', json={
        'wallet_id': wallet['id'], 'description': 'Constrained task', 'max_cost': '0.10',
        'minimum_quality': 0.90,
    }).json()
    decision = client.post(f"/missions/{mission['id']}/decide").json()
    assert decision['status'] == 'HOLD'
    assert decision['reason'] == 'NO_QUALIFIED_MODEL_WITHIN_ECONOMIC_ENVELOPE'


def test_execution_fails_closed_on_minimum_liquidity():
    wallet = client.post('/wallets', json={'capital': '1.00', 'minimum_liquidity': '0.90'}).json()
    model = client.post('/models', json={
        'id': 'liquidity-model', 'name': 'Liquidity', 'task_types': ['general'],
        'quality': 0.90, 'reliability': 1.0, 'cost_per_task': '0.20', 'latency_ms': 1000,
    }).json()
    mission = client.post('/missions', json={
        'wallet_id': wallet['id'], 'description': 'Protected task', 'max_cost': '0.20',
        'minimum_quality': 0.80,
    }).json()
    response = client.post('/executions', json={
        'mission_id': mission['id'], 'model_id': model['id'], 'execution_id': 'exec-liquidity',
        'cpu_seconds': 0, 'peak_memory_bytes': 0, 'io_read_bytes': 0, 'io_write_bytes': 0,
        'token_count': 200000,
        'outcome': 'verified_success',
    })
    assert response.status_code == 403
    assert 'minimum liquidity' in response.json()['detail']
