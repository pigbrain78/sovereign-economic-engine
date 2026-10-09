import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.crypto_provenance.canonical import CanonicalError, canonical_json
from app.crypto_provenance.provenance import agent_run_digest, sign_agent_run, verify_agent_run


def test_canonicalization_rejects_non_finite_values():
    with pytest.raises(CanonicalError):
        canonical_json({'value': float('nan')})


def test_ed25519_agent_run_signature_verifies(monkeypatch):
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes_raw()
    public_raw = private.public_key().public_bytes_raw()
    monkeypatch.setenv('SOVEREIGN_SIGNING_MODE', 'ed25519')
    monkeypatch.setenv('SOVEREIGN_SIGNING_KEY', base64.b64encode(private_raw).decode())
    monkeypatch.setenv('SOVEREIGN_VERIFY_KEY', base64.b64encode(public_raw).decode())
    payload = {'execution_id': 'run-1', 'status': 'COMPLETED', 'amount': '0.010000'}
    signature = sign_agent_run(payload)
    assert signature
    assert verify_agent_run(payload, signature)
    assert not verify_agent_run({**payload, 'amount': '0.020000'}, signature)
    assert len(agent_run_digest(payload)) == 64
