from __future__ import annotations

import os
from typing import Any

from .canonical import canonical_json, digest
from .key_vault import KeyVaultProvider


def canonical_agent_run(payload: dict[str, Any]) -> str:
    return canonical_json(payload)


def agent_run_digest(payload: dict[str, Any]) -> str:
    return digest(payload, 'sovereign.agent-run')


def signing_algorithm() -> str:
    mode = os.environ.get('SOVEREIGN_SIGNING_MODE', '').strip().lower()
    return 'ed25519' if mode == 'ed25519' else 'hash-only'


def sign_agent_run(payload: dict[str, Any]) -> str | None:
    if signing_algorithm() != 'ed25519':
        return None
    return KeyVaultProvider.sign_payload(canonical_agent_run(payload))


def verify_agent_run(payload: dict[str, Any], signature: str | None) -> bool:
    if signing_algorithm() != 'ed25519' or not signature:
        return False
    return KeyVaultProvider.verify_signature(canonical_agent_run(payload), signature)
