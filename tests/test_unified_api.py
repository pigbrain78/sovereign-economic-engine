import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ['SOVEREIGN_DB'] = '/tmp/sovereign-unified-api.db'
os.environ['SOVEREIGN_MEMORY_DB'] = '/tmp/sovereign-unified-memory.db'
for path in ('/tmp/sovereign-unified-api.db', '/tmp/sovereign-unified-memory.db'):
    Path(path).unlink(missing_ok=True)

from app.main import app  # noqa: E402

client = TestClient(app)


def test_unified_micro_billing_and_memory_flow():
    funded = client.post('/micro/wallets/payer/fund', json={'amount_micro_cents': 1000})
    assert funded.status_code == 200
    assert funded.json()['balance_micro_cents'] == 1000

    hold = client.post('/micro/escrows', json={'task_id': 'api-task-1', 'payer_agent_id': 'payer', 'lock_amount_micro_cents': 1000})
    assert hold.status_code == 201
    assert hold.json()['status'] == 'ACTIVE'

    telemetry = client.post('/micro/telemetry', json={
        'event_id': 'api-event-1', 'task_id': 'api-task-1', 'payer_agent_id': 'payer',
        'provider_agent_id': 'provider', 'metric_type': 'LLM_OUTPUT_TOKENS',
        'quantity': 25, 'unit_price_micro_cents': 2, 'total_charge_micro_cents': 50,
        'timestamp': '2026-10-01T00:00:00+00:00',
    })
    assert telemetry.status_code == 200
    assert telemetry.json()['consumed_amount_micro_cents'] == 50

    settled = client.post('/micro/escrows/api-task-1/settle', json={'contract_id': 'api-contract', 'rules': [
        {'beneficiary_id': 'provider', 'basis_points': 7000, 'role': 'MODEL_PROVIDER'},
        {'beneficiary_id': 'platform', 'basis_points': 3000, 'role': 'PLATFORM_FEE'},
    ]})
    assert settled.status_code == 200
    assert settled.json()['payload']['refunded_amount_micro_cents'] == 950

    remembered = client.post('/memory/remember', json={'content': 'Use verified outcomes per unit resource.', 'memory_type': 'principle', 'actor': 'system'})
    assert remembered.status_code == 201
    found = client.post('/memory/search', json={'query': 'verified outcomes'}).json()
    assert len(found) == 1
    assert client.get('/memory/health').json()['ledger_verified'] is True
