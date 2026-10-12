import base64
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.tool_discovery import canonical_manifest, evaluate_discovery, manifest_digest
from fastapi.testclient import TestClient
from app.main import app


def manifest(source_url='https://tools.example.test/manifest.json'):
    value = {
        'tool_id': 'tool.example.search',
        'version': '1.0.0',
        'name': 'Example Search',
        'purpose': 'Search approved public documentation',
        'provider': 'example',
        'source_url': source_url,
        'capabilities': ['search'],
        'permissions': {'network': 'read_only', 'filesystem': 'none'},
        'risk_level': 'low',
    }
    value['content_hash'] = manifest_digest(value)
    return value


def test_discovery_is_unverified_without_configured_attestation_key(monkeypatch):
    monkeypatch.delenv('TOOL_DISCOVERY_PUBLIC_KEY_B64', raising=False)
    result = evaluate_discovery(manifest())
    assert result['status'] == 'UNVERIFIED'
    assert result['eligible_for_proposal'] is False


def test_discovery_rejects_private_source_and_digest_tampering():
    private = evaluate_discovery(manifest('https://127.0.0.1/tool.json'))
    assert private['status'] == 'REJECTED'
    assert 'private' in private['reason']
    tampered = manifest()
    tampered['purpose'] = 'changed after attestation'
    result = evaluate_discovery(tampered)
    assert result['status'] == 'REJECTED'
    assert result['reason'] == 'CONTENT_HASH_MISMATCH'


def test_discovery_accepts_only_a_valid_ed25519_attestation(monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    monkeypatch.setenv('TOOL_DISCOVERY_PUBLIC_KEY_B64', public)
    candidate = manifest()
    signature = base64.b64encode(key.sign(canonical_manifest(candidate).encode())).decode()
    verified = evaluate_discovery(candidate, signature)
    assert verified['status'] == 'VERIFIED'
    assert verified['eligible_for_proposal'] is True
    bad = evaluate_discovery(candidate, signature[:-2] + 'aa')
    assert bad['status'] == 'UNVERIFIED'
    assert bad['eligible_for_proposal'] is False


def test_discovery_api_persists_unverified_catalog_state(monkeypatch):
    monkeypatch.delenv('TOOL_DISCOVERY_PUBLIC_KEY_B64', raising=False)
    candidate = manifest()
    candidate['tool_id'] = f"tool.example.api.{uuid.uuid4().hex[:10]}"
    candidate['content_hash'] = manifest_digest(candidate)
    response = TestClient(app).post('/tools/discover', json={'manifest': candidate})
    assert response.status_code == 201
    body = response.json()
    assert body['status'] == 'UNVERIFIED'
    assert body['eligible_for_proposal'] is False
    assert body['execution_authorized'] is False
