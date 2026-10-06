import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ['SOVEREIGN_DB'] = '/tmp/sovereign-nexus-api.db'
os.environ['SOVEREIGN_MEMORY_DB'] = '/tmp/sovereign-nexus-memory.db'
os.environ['SOVEREIGN_FACTORY_LEDGER'] = '/tmp/sovereign-nexus-factory-ledger.jsonl'
for path in (
    '/tmp/sovereign-nexus-api.db',
    '/tmp/sovereign-nexus-memory.db',
    '/tmp/sovereign-nexus-factory-ledger.jsonl',
):
    Path(path).unlink(missing_ok=True)

from app.main import app  # noqa: E402

client = TestClient(app)


def _approved_governance_proposal() -> str:
    project = client.post('/pocket/projects', json={'name': 'Nexus Gov', 'description': 'governance proof source'}).json()
    proposal = client.post(
        '/pocket/proposals',
        json={
            'project_id': project['project_id'],
            'agent_id': 'builder-agent',
            'title': 'Governed command',
            'category': 'governance',
            'candidate': {'scope': 'nexus'},
            'proposed_cost': '0.01',
            'recurring': False,
            'access_level': 'read_only',
            'expected_benefit': {'metric': 'safety'},
            'evidence': [{'proof': 'human-review'}],
            'recommendation': 'APPROVED_TO_PURCHASE',
        },
    ).json()
    decision = client.post(f"/pocket/proposals/{proposal['proposal_id']}/decision", json={'decision': 'approve', 'actor': 'user'})
    assert decision.status_code == 200
    return proposal['proposal_id']


def test_nexus_event_spine_stream_and_replay_projection():
    with client.websocket_connect('/nexus/stream') as ws:
        wallet = client.post('/wallets', json={'capital': '5.00', 'minimum_liquidity': '1.00'})
        assert wallet.status_code == 201
        ws.send_text('ping')

    events = client.get('/nexus/events', params={'domain': 'economic'}).json()
    assert len(events) >= 1
    assert events[-1]['event_type'] == 'WALLET_CREATED'

    projection = client.post('/nexus/replay/projection', json={'block_height': events[-1]['block_height']})
    assert projection.status_code == 200
    assert len(projection.json()['wallets']) >= 1


def test_gavel_workflow_enforces_signed_lease_then_executes():
    wallet = client.post('/wallets', json={'capital': '10.00', 'minimum_liquidity': '1.00'}).json()
    model = client.post(
        '/models',
        json={
            'name': 'Gavel Model',
            'task_types': ['general'],
            'quality': 0.95,
            'reliability': 0.95,
            'cost_per_task': '0.50',
            'latency_ms': 700,
        },
    ).json()

    create_action = client.post(
        '/nexus/gavel/actions',
        json={
            'action_type': 'EXECUTE_MISSION',
            'entity_ref': 'mission-pipeline',
            'confidence': 0.90,
            'estimated_cost': '120.00',
            'risk_level': 'HIGH',
            'payload': {
                'wallet_id': wallet['id'],
                'task_intent': 'Governed mission',
                'budget': '1.00',
                'minimum_quality': 0.8,
                'task_type': 'general',
                'required_model_id': model['id'],
                'token_count': 1000,
            },
            'requires_human': True,
        },
    )
    assert create_action.status_code == 201
    action_id = create_action.json()['action_id']

    missing_signature = client.post(
        f'/nexus/gavel/actions/{action_id}/decision',
        json={'decision': 'approve', 'actor': 'user'},
    )
    assert missing_signature.status_code == 422

    approved = client.post(
        f'/nexus/gavel/actions/{action_id}/decision',
        json={
            'decision': 'approve',
            'actor': 'user',
            'signer_key_id': 'POCKET-OS-KEY-01',
            'signature': 'ed25519-sig',
            'nonce': f'nonce-{action_id}',
            'lease_expires_at': '2099-01-01T00:00:00+00:00',
        },
    )
    assert approved.status_code == 200
    assert approved.json()['status'] == 'APPROVED'

    executed = client.post(f'/nexus/gavel/actions/{action_id}/execute', json={'actor': 'user'})
    assert executed.status_code == 201
    assert executed.json()['action']['status'] == 'EXECUTED'


def test_graph_shadow_market_mesh_and_rollback_governed_command():
    opportunity = client.post(
        '/nexus/shadow/opportunities',
        json={
            'title': 'Optimize token burn',
            'summary': 'High token burn for low-value prompts',
            'domain': 'economy',
            'expected_roi': 1.5,
            'evidence': [{'metric': 'token_burn', 'value': 1234}],
        },
    )
    assert opportunity.status_code == 201

    governance_proposal_id = _approved_governance_proposal()
    wallet = client.post('/wallets', json={'capital': '3.00', 'minimum_liquidity': '0.50'}).json()
    swarm = client.post(
        '/nexus/shadow/swarm-requests',
        json={
            'wallet_id': wallet['id'],
            'opportunity_id': opportunity.json()['opportunity_id'],
            'budget': '0.50',
            'target_roi': 1.2,
            'governance_proposal_id': governance_proposal_id,
            'actor': 'user',
        },
    )
    assert swarm.status_code == 201

    market = client.get('/nexus/shadow/market')
    assert market.status_code == 200
    assert len(market.json()['opportunities']) >= 1

    queue = client.post(
        '/nexus/mesh/outbound',
        json={'partition_id': 'local-lan', 'event_type': 'STATE_DELTA', 'payload': {'k': 'v'}, 'local_merkle_root': 'abc12345'},
    )
    assert queue.status_code == 201
    queue_id = queue.json()['queue_id']
    reconciled = client.post(
        f'/nexus/mesh/outbound/{queue_id}/reconcile',
        json={'remote_merkle_root': 'def67890', 'status': 'MERGED', 'conflict_note': ''},
    )
    assert reconciled.status_code == 200
    assert reconciled.json()['status'] == 'MERGED'

    graph = client.get('/nexus/graph')
    assert graph.status_code == 200
    assert len(graph.json()['nodes']) >= 1

    timeline = client.get('/nexus/replay/timeline').json()
    target_height = timeline[-1]['block_height']
    rollback = client.post(
        '/nexus/replay/rollback-commands',
        json={
            'target_block_height': target_height,
            'governance_proposal_id': governance_proposal_id,
            'actor': 'user',
            'reason': 'deterministic recovery drill',
        },
    )
    assert rollback.status_code == 201
    assert rollback.json()['status'] == 'QUEUED_GOVERNED_COMMAND'
