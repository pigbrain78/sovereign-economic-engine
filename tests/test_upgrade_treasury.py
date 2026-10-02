import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.main import app
from app.upgrade_treasury import canonical_approval_payload

client = TestClient(app)


def approval_key():
    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    os.environ['TREASURY_APPROVAL_PUBLIC_KEY_B64'] = public_b64
    return private


def approve(proposal, private):
    actor = 'operator'
    payload = canonical_approval_payload(proposal['proposal_id'], proposal['wallet_id'], __import__('decimal').Decimal(proposal['estimated_cost']), actor)
    signature = base64.b64encode(private.sign(payload)).decode()
    return client.post(f"/upgrades/proposals/{proposal['proposal_id']}/approve", json={'actor': actor, 'signature_b64': signature})


def test_deposit_split_and_upgrade_lifecycle():
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    assert wallet['available'] == '8.000000'
    assert wallet['development_reserve'] == '1.000000'
    assert wallet['safety_reserve'] == '0.500000'
    assert wallet['owner_reserve'] == '0.500000'

    deposited = client.post(f"/wallets/{wallet['id']}/deposit", json={'amount': '10.00'}).json()
    assert deposited['total'] == '20.000000'
    assert deposited['development_reserve'] == '2.000000'
    assert deposited['available'] == '16.000000'

    proposal = client.post('/upgrades/proposals', json={
        'wallet_id': wallet['id'], 'target_module': 'local_model_router', 'estimated_cost': '0.50',
        'predicted_monthly_value': '3.00', 'scenario_paths': [{'path_id': 'p1', 'decision_node': 'router', 'confidence': .9, 'monetary_exposure': '0.50', 'expected_benefit': '3.00'}],
    }).json()
    assert proposal['status'] == 'PENDING_EVALUATION'

    evaluated = client.post(f"/upgrades/proposals/{proposal['proposal_id']}/evaluate", json={
        'mutation_id': 'mut-1', 'ast_node_coverage': .92, 'failure_reduction_rate': .35, 'fitness_score': .84,
        'verifier_passed': True, 'evidence': {'tests': '42 passed'},
    }).json()
    assert evaluated['status'] == 'PENDING_HUMAN_GATE'

    private = approval_key()
    approved = approve(evaluated, private)
    assert approved.status_code == 200
    assert approved.json()['status'] == 'FUNDS_RESERVED'
    assert approved.json()['signature_verified'] is True
    assert client.get(f"/wallets/{wallet['id']}").json()['development_reserve'] == '1.500000'

    settled = client.post(f"/upgrades/proposals/{proposal['proposal_id']}/complete", json={'verified': True, 'actual_monthly_value': '3.50', 'evidence': {'differential': 'zero-delta'}})
    assert settled.status_code == 200
    assert settled.json()['status'] == 'SETTLED'
    assert settled.json()['actual_roi'] == 6.0

    events = client.get('/upgrades/events').json()
    assert events['ledger_verified'] is True
    assert [event['event_type'] for event in events['events'][-4:]] == ['PROPOSAL_CREATED', 'EVALUATION_COMPLETED', 'FUNDS_RESERVED', 'UPGRADE_SETTLED']


def test_failed_fitness_and_invalid_signature_fail_closed():
    wallet = client.post('/wallets', json={'capital': '5.00'}).json()
    proposal = client.post('/upgrades/proposals', json={'wallet_id': wallet['id'], 'target_module': 'unsafe_mutation', 'estimated_cost': '0.40'}).json()
    failed = client.post(f"/upgrades/proposals/{proposal['proposal_id']}/evaluate", json={
        'mutation_id': 'mut-bad', 'ast_node_coverage': .2, 'failure_reduction_rate': .01, 'fitness_score': .4,
        'verifier_passed': False,
    })
    assert failed.status_code == 200
    assert failed.json()['status'] == 'EVALUATION_FAILED'

    good = client.post('/upgrades/proposals', json={'wallet_id': wallet['id'], 'target_module': 'safe_mutation', 'estimated_cost': '0.40'}).json()
    client.post(f"/upgrades/proposals/{good['proposal_id']}/evaluate", json={
        'mutation_id': 'mut-good', 'ast_node_coverage': .9, 'failure_reduction_rate': .2, 'fitness_score': .8,
        'verifier_passed': True,
    })
    approval_key()
    invalid = client.post(f"/upgrades/proposals/{good['proposal_id']}/approve", json={'actor': 'operator', 'signature_b64': base64.b64encode(b'bad').decode()})
    assert invalid.status_code == 403
    assert client.get(f"/wallets/{wallet['id']}").json()['development_reserve'] == wallet['development_reserve']
