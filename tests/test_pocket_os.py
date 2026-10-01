from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_pocket_builder_proposal_requires_evidence_and_human_decision():
    project = client.post('/pocket/projects', json={
        'name': 'Pocket OS Treasury',
        'description': 'Improve the economic engine with evidence-backed tools.',
    })
    assert project.status_code == 201
    project_id = project.json()['project_id']

    loop = client.post(f'/pocket/projects/{project_id}/loops', json={
        'title': 'Reduce regression-test time',
        'objective': 'Evaluate tools that improve test speed without reducing coverage.',
    })
    assert loop.status_code == 201

    shadow = client.post(f'/pocket/projects/{project_id}/shadow', json={
        'kind': 'opportunity',
        'content': 'A parallel test runner may reduce runtime.',
        'confidence': 0.78,
        'evidence': [{'metric': 'test_runtime_seconds', 'baseline': 42}],
    })
    assert shadow.status_code == 201
    assert shadow.json()['authority'] == 'NONE'
    assert shadow.json()['can_execute'] is False
    assert shadow.json()['can_ratify'] is False

    no_evidence = client.post('/pocket/proposals', json={
        'project_id': project_id,
        'agent_id': 'builder-agent',
        'title': 'Buy testing platform',
        'category': 'testing',
        'candidate': {'vendor': 'Example'},
        'proposed_cost': '9.99',
        'recurring': True,
        'access_level': 'read_only',
        'expected_benefit': {'metric': 'runtime', 'target': 30},
        'recommendation': 'APPROVED_TO_PURCHASE',
    })
    assert no_evidence.status_code == 422

    proposal = client.post('/pocket/proposals', json={
        'project_id': project_id,
        'loop_id': loop.json()['loop_id'],
        'shadow_id': shadow.json()['shadow_id'],
        'agent_id': 'builder-agent',
        'title': 'Evaluate testing platform',
        'category': 'testing',
        'candidate': {'vendor': 'Example', 'url': 'https://example.test'},
        'proposed_cost': '9.99',
        'recurring': True,
        'access_level': 'read_only',
        'expected_benefit': {'metric': 'test_runtime_seconds', 'baseline': 42, 'candidate': 29.4},
        'evidence': [{'runs': 100, 'success_rate': 0.96}],
        'recommendation': 'APPROVED_TO_PURCHASE',
    })
    assert proposal.status_code == 201
    assert proposal.json()['status'] == 'HOLD_FOR_APPROVAL'
    assert proposal.json()['execution_authorized'] is False

    approved = client.post(f"/pocket/proposals/{proposal.json()['proposal_id']}/decision", json={
        'decision': 'approve', 'actor': 'user',
    })
    assert approved.status_code == 200
    assert approved.json()['status'] == 'USER_APPROVED_PENDING_EXTERNAL_PURCHASE'
    assert approved.json()['execution_authorized'] is False
    assert 'does not purchase' in approved.json()['note']


def test_shadow_cannot_approve_or_execute():
    project = client.post('/pocket/projects', json={'name': 'Shadow boundary test', 'description': 'Verify advisory authority boundaries.'}).json()
    shadow = client.post(f"/pocket/projects/{project['project_id']}/shadow", json={
        'kind': 'recommendation', 'content': 'Try a local benchmark.', 'confidence': 0.5,
        'evidence': [{'source': 'test'}],
    }).json()
    assert shadow['authority'] == 'NONE'
    assert shadow['can_execute'] is False and shadow['can_ratify'] is False

    proposal = client.post('/pocket/proposals', json={
        'project_id': project['project_id'], 'shadow_id': shadow['shadow_id'], 'agent_id': 'builder-agent',
        'title': 'Boundary test proposal', 'category': 'testing', 'candidate': {'vendor': 'local'},
        'proposed_cost': '1.00', 'recurring': False, 'access_level': 'read_only',
        'expected_benefit': {'metric': 'coverage', 'candidate': 0.8},
        'evidence': [{'runs': 3}], 'recommendation': 'HOLD_FOR_APPROVAL',
    }).json()
    approved = client.post(f"/pocket/proposals/{proposal['proposal_id']}/decision", json={'decision': 'approve', 'actor': 'user'})
    assert approved.status_code == 200
    replay = client.post(f"/pocket/proposals/{proposal['proposal_id']}/decision", json={'decision': 'approve', 'actor': 'shadow'})
    assert replay.status_code == 409

    events = client.get('/pocket/events').json()
    assert any(event['event_type'] == 'PROPOSAL_DECIDED' for event in events)
