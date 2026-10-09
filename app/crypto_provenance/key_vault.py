"""Fail-closed signing provider with legacy HMAC and Ed25519 support."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature


class KeyVaultProvider:
    """Load signing material from the environment and fail closed on errors."""

    @staticmethod
    def get_key(key_name: str) -> str:
        key = os.environ.get(key_name)
        if not key or not key.strip():
            raise RuntimeError(
                f"CRITICAL_SECURITY_ABORT: Key '{key_name}' missing or empty."
            )
        return key.strip()

    @staticmethod
    def mode() -> str:
        return os.environ.get("SOVEREIGN_SIGNING_MODE", "hmac").strip().lower()

    @classmethod
    def is_asymmetric(cls) -> bool:
        return cls.mode() == "ed25519"

    @classmethod
    def _decode_raw_key(cls, key_name: str, expected_size: int) -> bytes:
        try:
            raw = base64.b64decode(cls.get_key(key_name), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(f"CRITICAL_SECURITY_ABORT: Invalid Base64 in '{key_name}'") from exc
        if len(raw) != expected_size:
            raise RuntimeError(
                f"CRITICAL_SECURITY_ABORT: '{key_name}' must decode to {expected_size} bytes"
            )
        return raw

    @classmethod
    def _load_private(cls, key_name: str = "SOVEREIGN_SIGNING_KEY") -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(cls._decode_raw_key(key_name, 32))

    @classmethod
    def _load_public(cls, key_name: str = "SOVEREIGN_VERIFY_KEY") -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(cls._decode_raw_key(key_name, 32))

    @classmethod
    def sign_payload(cls, payload: str, key_name: str = "SOVEREIGN_SIGNING_KEY") -> str:
        if cls.is_asymmetric():
            signature = cls._load_private(key_name).sign(payload.encode("utf-8"))
            return base64.b64encode(signature).decode("ascii")
        key = cls.get_key(key_name).encode("utf-8")
        return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def verify_signature(
        cls,
        payload: str,
        signature: str,
        key_name: str = "SOVEREIGN_VERIFY_KEY",
        hmac_key_name: str = "SOVEREIGN_SIGNING_KEY",
    ) -> bool:
        if cls.is_asymmetric():
            try:
                raw_signature = base64.b64decode(signature, validate=True)
                if len(raw_signature) != 64:
                    return False
                cls._load_public(key_name).verify(raw_signature, payload.encode("utf-8"))
                return True
            except (binascii.Error, InvalidSignature, ValueError, TypeError):
                return False
        key = cls.get_key(hmac_key_name).encode("utf-8")
        expected = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    @classmethod
    def public_key_base64(cls, key_name: str = "SOVEREIGN_VERIFY_KEY") -> str:
        """Return a normalized public key after validating its shape."""
        raw = cls._decode_raw_key(key_name, 32)
        return base64.b64encode(raw).decode("ascii")
