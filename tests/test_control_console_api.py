import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ['SOVEREIGN_DB'] = '/tmp/sovereign-console-api.db'
os.environ['SOVEREIGN_MEMORY_DB'] = '/tmp/sovereign-console-memory.db'
for path in ('/tmp/sovereign-console-api.db', '/tmp/sovereign-console-memory.db'):
    Path(path).unlink(missing_ok=True)

from app.main import app  # noqa: E402

client = TestClient(app)


def test_skill_lifecycle_is_visible_and_governance_gated():
    skill = client.post('/skills/candidates', json={
        'name': 'Tax skill candidate',
        'description': 'Candidate for governed promotion',
        'status': 'candidate',
        'metadata': {'domain': 'finance'},
        'lineage': {'parent': None},
        'provenance': {'source': 'operator'},
        'evidence': [{'type': 'registration'}],
    })
    assert skill.status_code == 201
    skill_id = skill.json()['skill_id']

    blocked = client.post(f'/skills/{skill_id}/admit', json={
        'governance_proposal_id': 'missing',
        'actor': 'user',
    })
    assert blocked.status_code == 409

    qualified = client.post(f'/skills/{skill_id}/qualify', json={
        'tool_code': 'def compute_tax(amount):\n    return amount * 0.15\n',
        'test_code': 'import tool\n\ndef test_tax():\n    assert tool.compute_tax(100) == 15.0\n',
        'target_file': 'financials/tax.py',
    })
    assert qualified.status_code == 200
    assert qualified.json()['status'] == 'qualified'

    project = client.post('/pocket/projects', json={
        'name': 'Skill governance',
        'description': 'Governed skill review',
    }).json()
    proposal = client.post('/pocket/proposals', json={
        'project_id': project['project_id'],
        'agent_id': 'builder-agent',
        'title': 'Admit skill',
        'category': 'skill',
        'candidate': {'skill_id': skill_id},
        'proposed_cost': '0.01',
        'recurring': False,
        'access_level': 'read_only',
        'expected_benefit': {'metric': 'correctness'},
        'evidence': [{'source': 'factory-evaluation'}],
        'recommendation': 'APPROVED_TO_PURCHASE',
    }).json()
    decision = client.post(f"/pocket/proposals/{proposal['proposal_id']}/decision", json={'decision': 'approve', 'actor': 'user'})
    assert decision.status_code == 200

    admitted = client.post(f'/skills/{skill_id}/admit', json={
        'governance_proposal_id': proposal['proposal_id'],
        'actor': 'user',
    })
    assert admitted.status_code == 200
    assert admitted.json()['status'] == 'admitted'

    detail = client.get(f'/skills/{skill_id}')
    assert detail.status_code == 200
    assert any(event['event_type'] == 'SKILL_ADMITTED' for event in detail.json()['events'])


def test_console_mission_execution_runs_economic_lifecycle():
    wallet = client.post('/wallets', json={'capital': '10.00', 'minimum_liquidity': '1.00'}).json()
    model = client.post('/models', json={
        'name': 'Console model',
        'task_types': ['general'],
        'quality': 0.95,
        'reliability': 0.95,
        'cost_per_task': '0.50',
        'latency_ms': 700,
    }).json()
    run = client.post('/console/missions/execute', json={
        'wallet_id': wallet['id'],
        'task_intent': 'Run governed mission',
        'budget': '1.00',
        'minimum_quality': 0.80,
        'task_type': 'general',
        'required_model_id': model['id'],
        'token_count': 120000,
        'resource_constraints': {'max_tokens': 150000},
        'quality_constraints': {'min_quality': 0.8},
    })
    assert run.status_code == 201
    body = run.json()
    assert body['routing']['status'] == 'APPROVED'
    assert body['execution']['outcome'] == 'verified_success'

    events = [row['event'] for row in client.get(f"/ledger/{wallet['id']}").json()]
    assert events == ['deposit', 'reserve', 'outcome', 'release']


def test_console_ui_and_demo_observability():
    ui = client.get('/console')
    assert ui.status_code == 200
    assert 'AI Economic Control Console' in ui.text

    demo = client.post('/console/demo/run', json={})
    assert demo.status_code == 201

    snapshot = client.get('/console/observability')
    assert snapshot.status_code == 200
    data = snapshot.json()
    assert len(data['wallets']) >= 1
    assert len(data['missions']) >= 1
    assert len(data['ledger']) >= 1
