import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ['SOVEREIGN_DB'] = '/tmp/sovereign-capture-api.db'
os.environ['SOVEREIGN_MEMORY_DB'] = '/tmp/sovereign-capture-memory.db'
for path in ('/tmp/sovereign-capture-api.db', '/tmp/sovereign-capture-memory.db'):
    Path(path).unlink(missing_ok=True)

from app.main import app  # noqa: E402

client = TestClient(app)


def test_capture_creates_memory_mission_and_bounded_local_envelope():
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    response = client.post('/capture', json={
        'wallet_id': wallet['id'],
        'content': 'Find a way to reduce the test time in my project.',
        'privacy_mode': 'local_only',
        'max_cost': '0.05',
        'capabilities': ['project_analysis', 'test_analysis'],
        'actor': 'operator',
    })
    assert response.status_code == 201
    body = response.json()
    assert body['memory']['memory_type'] == 'thought_capture'
    assert body['mission']['status'] == 'planned'
    assert body['envelope']['status'] == 'AWAITING_EXECUTION'
    assert body['envelope']['runtime_policy'] == 'local'
    assert body['execution_authorized'] is False
    assert body['escrow_reserved'] is False
    assert body['envelope']['quote_hash']
    fetched = client.get(f"/capture/{body['envelope']['envelope_id']}")
    assert fetched.status_code == 200
    assert fetched.json()['memory']['memory_id'] == body['memory']['memory_id']


def test_capture_replay_is_idempotent_and_does_not_charge():
    wallet = client.post('/wallets', json={'capital': '5.00'}).json()
    payload = {'wallet_id': wallet['id'], 'content': 'Replay-safe capture', 'privacy_mode': 'approved_external', 'max_cost': '0.10'}
    first = client.post('/capture', json=payload).json()
    replay = client.post('/capture', json=payload).json()
    assert replay['idempotent_replay'] is True
    assert replay['envelope']['envelope_id'] == first['envelope']['envelope_id']
    current = client.get(f"/wallets/{wallet['id']}").json()
    assert current['reserved'] == '0.000000'
    assert current['spent'] == '0.000000'


def test_capture_rejects_zero_budget_and_unknown_wallet():
    response = client.post('/capture', json={'wallet_id': 'missing', 'content': 'x', 'max_cost': '0'})
    assert response.status_code == 422
    response = client.post('/capture', json={'wallet_id': 'missing', 'content': 'x', 'max_cost': '0.01'})
    assert response.status_code == 404
