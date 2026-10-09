import json
import os
from pathlib import Path

os.environ['SOVEREIGN_DB'] = '/tmp/sovereign-agent-runs.db'
os.environ['SOVEREIGN_MEMORY_DB'] = '/tmp/sovereign-agent-memory.db'
for path in ('/tmp/sovereign-agent-runs.db', '/tmp/sovereign-agent-memory.db'):
    Path(path).unlink(missing_ok=True)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_agent_run_links_reservation_settlement_and_provenance():
    os.environ['LOCAL_EXECUTOR_COMMAND_JSON'] = json.dumps(['python3', '-c', 'import sys; print("agent:" + sys.stdin.read())'])
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    capture = client.post('/capture', json={'wallet_id': wallet['id'], 'content': 'Improve the build loop', 'privacy_mode': 'local_only', 'max_cost': '0.05'}).json()
    response = client.post(f"/capture/{capture['envelope']['envelope_id']}/execute-local", json={'execution_id': 'agent-run-success'})
    assert response.status_code == 200
    body = response.json()
    run = body['agent_run']
    assert run['execution_id'] == 'agent-run-success'
    assert run['authorization_status'] == 'AUTHORIZED'
    assert run['status'] == 'COMPLETED'
    assert run['reservation_id'].startswith('local_reservation_')
    assert run['output_hash']
    assert len(run['provenance_hash']) == 64
    assert client.get('/agent-runs/agent-run-success').json()['usage']['token_count'] >= 1


def test_agent_run_replay_does_not_require_runtime_or_double_settle():
    os.environ['LOCAL_EXECUTOR_COMMAND_JSON'] = json.dumps(['python3', '-c', 'print("once")'])
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    capture = client.post('/capture', json={'wallet_id': wallet['id'], 'content': 'Replay safely', 'privacy_mode': 'local_only', 'max_cost': '0.05'}).json()
    first = client.post(f"/capture/{capture['envelope']['envelope_id']}/execute-local", json={'execution_id': 'agent-run-replay'}).json()
    os.environ.pop('LOCAL_EXECUTOR_COMMAND_JSON', None)
    replay = client.post(f"/capture/{capture['envelope']['envelope_id']}/execute-local", json={'execution_id': 'agent-run-replay'}).json()
    assert replay['idempotent_replay'] is True
    assert replay['agent_run']['provenance_hash'] == first['agent_run']['provenance_hash']
    assert replay['amount'] == first['amount']


def test_agent_run_records_failed_execution_after_refund():
    os.environ['LOCAL_EXECUTOR_COMMAND_JSON'] = json.dumps(['python3', '-c', 'import sys; print("bad", file=sys.stderr); sys.exit(2)'])
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    capture = client.post('/capture', json={'wallet_id': wallet['id'], 'content': 'Handle failure', 'privacy_mode': 'local_only', 'max_cost': '0.05'}).json()
    response = client.post(f"/capture/{capture['envelope']['envelope_id']}/execute-local", json={'execution_id': 'agent-run-failure'})
    assert response.status_code == 200
    run = response.json()['agent_run']
    assert run['status'] == 'FAILED_REFUNDED'
    assert run['amount'] == '0.000000'
    assert run['error']
