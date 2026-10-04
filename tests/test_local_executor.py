import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ['SOVEREIGN_DB'] = '/tmp/sovereign-local-executor.db'
os.environ['SOVEREIGN_MEMORY_DB'] = '/tmp/sovereign-local-memory.db'
for path in ('/tmp/sovereign-local-executor.db', '/tmp/sovereign-local-memory.db'):
    Path(path).unlink(missing_ok=True)

from app.main import app  # noqa: E402

client = TestClient(app)


def test_local_execution_requires_explicit_configuration():
    os.environ.pop('LOCAL_EXECUTOR_COMMAND_JSON', None)
    assert client.get('/local-executor/status').json()['configured'] is False
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    capture = client.post('/capture', json={'wallet_id': wallet['id'], 'content': 'Analyze my test suite', 'privacy_mode': 'local_only', 'max_cost': '0.05'}).json()
    response = client.post(f"/capture/{capture['envelope']['envelope_id']}/execute-local", json={'execution_id': 'local-unconfigured'})
    assert response.status_code == 503


def test_local_execution_reserves_settles_and_replays():
    os.environ['LOCAL_EXECUTOR_COMMAND_JSON'] = json.dumps(['python3', '-c', 'import sys; print("LOCAL_RESULT:" + sys.stdin.read())'])
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    capture = client.post('/capture', json={'wallet_id': wallet['id'], 'content': 'Analyze my test suite', 'privacy_mode': 'local_only', 'max_cost': '0.05'}).json()
    envelope_id = capture['envelope']['envelope_id']
    response = client.post(f'/capture/{envelope_id}/execute-local', json={'execution_id': 'local-success'} )
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'COMPLETED'
    assert body['execution_authorized'] is True
    assert body['escrow_reserved'] is False
    assert body['output'].startswith('LOCAL_RESULT:Analyze my test suite')
    assert body['wallet']['reserved'] == '0.000000'
    replay = client.post(f'/capture/{envelope_id}/execute-local', json={'execution_id': 'local-success'}).json()
    assert replay['idempotent_replay'] is True
    assert replay['amount'] == body['amount']


def test_local_execution_rejects_external_privacy_mode():
    os.environ['LOCAL_EXECUTOR_COMMAND_JSON'] = json.dumps(['python3', '-c', 'print("should not run")'])
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    capture = client.post('/capture', json={'wallet_id': wallet['id'], 'content': 'External is not local', 'privacy_mode': 'approved_external', 'max_cost': '0.05'}).json()
    response = client.post(f"/capture/{capture['envelope']['envelope_id']}/execute-local", json={'execution_id': 'local-wrong-policy'})
    assert response.status_code == 403
