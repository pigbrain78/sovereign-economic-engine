import uuid

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_skill_forge_validation_and_proposal_bridge():
    suffix = uuid.uuid4().hex[:8]
    skill_id = f'skill.pocketos.api.{suffix}'
    genome = {
        'skill_id': skill_id,
        'version': '1.0.0',
        'name': 'API Test Skill',
        'purpose': 'Exercise the governed registry.',
        'domain': 'Pocket OS',
        'author': 'operator',
        'permissions': {'read': True, 'write': False, 'network': False, 'filesystem': True, 'financial': False, 'execCode': True},
        'governance': {'requires_approval': True, 'risk_level': 'medium', 'sail_approved': True},
    }
    registered = client.post('/forge/skills', json={'genome': genome})
    assert registered.status_code == 201
    validated = client.post(f'/forge/skills/{skill_id}/validate', json={'report': {'passed': True, 'functional_score': 95, 'security_score': 95, 'governance_score': 95, 'compliance_score': 95, 'security_issues': []}})
    assert validated.status_code == 200
    assert validated.json()['decision']['status'] == 'approved'
    project = client.post('/pocket/projects', json={'name': f'Forge API {suffix}', 'description': 'Skill Forge bridge test.'}).json()
    proposed = client.post(f'/forge/skills/{skill_id}/proposals', json={'project_id': project['project_id'], 'agent_id': 'builder-agent', 'proposed_cost': '0.25', 'expected_benefit': {'metric': 'test_runtime'}, 'evidence': [{'validation_id': validated.json()['validation_id']}]})
    assert proposed.status_code == 201
    assert proposed.json()['status'] == 'HOLD_FOR_APPROVAL'
    assert proposed.json()['execution_authorized'] is False
