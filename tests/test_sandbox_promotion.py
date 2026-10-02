import base64
import os
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.main import app
import app.main as main_module
from app.sandbox_promotion import SandboxPromotion, SandboxRejected
from app.upgrade_treasury import canonical_approval_payload

client = TestClient(app)


def test_sandbox_rejects_traversal_and_invalid_python(tmp_path):
    sandbox = SandboxPromotion(tmp_path / 'sandbox')
    with pytest.raises(SandboxRejected):
        sandbox.stage('bad', {'../escape.py': 'print(1)'})
    destination, receipt = sandbox.stage('broken', {'module.py': 'def broken(:\n'})
    assert receipt.passed is False
    assert not destination.exists()


def test_sandbox_promote_and_rollback_restores_previous(tmp_path):
    sandbox = SandboxPromotion(tmp_path / 'sandbox')
    first_path, first = sandbox.stage('first', {'module.py': 'VALUE = 1\n'})
    first_receipt = sandbox.promote('first', first.candidate_hash)
    assert first_receipt.promoted is True
    second_path, second = sandbox.stage('second', {'module.py': 'VALUE = 2\n'})
    sandbox.promote('second', second.candidate_hash)
    rolled = sandbox.rollback(second.candidate_hash)
    assert rolled.promoted is False
    assert sandbox.active()['candidate_id'] == 'first'
    assert sandbox.active()['candidate_hash'] == first.candidate_hash


def _signed_approval(proposal):
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    os.environ['TREASURY_APPROVAL_PUBLIC_KEY_B64'] = public
    payload = canonical_approval_payload(proposal['proposal_id'], proposal['wallet_id'], Decimal(proposal['estimated_cost']), 'operator')
    signature = base64.b64encode(private.sign(payload)).decode()
    return {'actor': 'operator', 'signature_b64': signature}


def test_api_canary_gate_promote_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv('SOVEREIGN_SANDBOX_ROOT', str(tmp_path / 'api-sandbox'))
    main_module.SANDBOX = SandboxPromotion(tmp_path / 'api-sandbox')
    wallet = client.post('/wallets', json={'capital': '10.00'}).json()
    proposal = client.post('/upgrades/proposals', json={'wallet_id': wallet['id'], 'target_module': 'router', 'estimated_cost': '0.40'}).json()
    client.post(f"/upgrades/proposals/{proposal['proposal_id']}/evaluate", json={'mutation_id': 'mut', 'ast_node_coverage': .9, 'failure_reduction_rate': .2, 'fitness_score': .8, 'verifier_passed': True})
    assert client.post(f"/upgrades/proposals/{proposal['proposal_id']}/approve", json=_signed_approval(proposal)).status_code == 200

    staged = client.post(f"/upgrades/proposals/{proposal['proposal_id']}/promote", json={'candidate_id': 'candidate-1', 'files': {'router.py': 'VALUE = 2\n'}, 'canary_percent': 10})
    assert staged.status_code == 200
    assert staged.json()['verification']['passed'] is True
    blocked = client.post(f"/upgrades/proposals/{proposal['proposal_id']}/activate", json={'canary_passed': False, 'evidence': {'tests': 'failed'}})
    assert blocked.status_code == 422
    assert client.get(f"/upgrades/proposals/{proposal['proposal_id']}").json()['status'] == 'VERIFICATION_PENDING'
    assert client.get(f"/upgrades/proposals/{proposal['proposal_id']}/promotion").json()['status'] == 'CANARY_FAILED'

    # The failed canary leaves the candidate inactive; a fresh proposal proves the activation path.
    proposal2 = client.post('/upgrades/proposals', json={'wallet_id': wallet['id'], 'target_module': 'router2', 'estimated_cost': '0.40'}).json()
    client.post(f"/upgrades/proposals/{proposal2['proposal_id']}/evaluate", json={'mutation_id': 'mut2', 'ast_node_coverage': .9, 'failure_reduction_rate': .2, 'fitness_score': .8, 'verifier_passed': True})
    assert client.post(f"/upgrades/proposals/{proposal2['proposal_id']}/approve", json=_signed_approval(proposal2)).status_code == 200
    staged2 = client.post(f"/upgrades/proposals/{proposal2['proposal_id']}/promote", json={'candidate_id': 'candidate-2', 'files': {'router.py': 'VALUE = 3\n'}}).json()
    activated = client.post(f"/upgrades/proposals/{proposal2['proposal_id']}/activate", json={'canary_passed': True, 'evidence': {'tests': 'passed'}})
    assert activated.status_code == 200
    rolled = client.post(f"/upgrades/proposals/{proposal2['proposal_id']}/rollback", json={'reason': 'post-promotion regression'})
    assert rolled.status_code == 200
    assert rolled.json()['status'] == 'ROLLED_BACK'
