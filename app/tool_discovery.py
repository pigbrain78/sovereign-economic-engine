"""Fail-closed external tool discovery and proposal eligibility contracts.

This module does not fetch arbitrary URLs. A provider adapter must supply a
manifest and an attestation, while the server validates the source namespace,
content digest, and (when configured) an Ed25519 signature. Discovery and
eligibility are catalog decisions only; they never grant execution or purchase
authority.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
from urllib.parse import urlparse
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class ToolDiscoveryRejected(ValueError):
    pass


def canonical_manifest(manifest: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in manifest.items() if key not in {"content_hash", "signature_b64"}}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_manifest(manifest).encode("utf-8")).hexdigest()


def validate_source_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ToolDiscoveryRejected("source_url must be an HTTPS URL without credentials or fragments")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ToolDiscoveryRejected("private or local source namespace is not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise ToolDiscoveryRejected("private, loopback, link-local, or reserved source address is not allowed")


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("tool_id", "version", "name", "purpose", "provider", "source_url", "capabilities", "permissions", "risk_level", "content_hash"):
        if field not in manifest or manifest[field] in (None, "", []):
            errors.append(f"MISSING_MANIFEST_FIELD:{field}")
    if not isinstance(manifest.get("capabilities"), list) or not all(isinstance(item, str) and item for item in manifest.get("capabilities", [])):
        errors.append("INVALID_CAPABILITIES")
    if not isinstance(manifest.get("permissions"), dict):
        errors.append("INVALID_PERMISSIONS")
    if manifest.get("risk_level") not in {"low", "medium", "high", "critical"}:
        errors.append("INVALID_RISK_LEVEL")
    tool_id = manifest.get("tool_id")
    if not isinstance(tool_id, str) or len(tool_id) > 128 or not tool_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in tool_id):
        errors.append("INVALID_TOOL_ID")
    return errors


def verify_attestation(manifest: dict[str, Any], signature_b64: str | None) -> tuple[bool, str]:
    configured_key = os.environ.get("TOOL_DISCOVERY_PUBLIC_KEY_B64")
    if not configured_key:
        return False, "TOOL_DISCOVERY_PUBLIC_KEY_B64_NOT_CONFIGURED"
    if not signature_b64:
        return False, "ATTESTATION_SIGNATURE_MISSING"
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(configured_key, validate=True))
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature, canonical_manifest(manifest).encode("utf-8"))
    except (ValueError, InvalidSignature):
        return False, "ATTESTATION_SIGNATURE_INVALID"
    return True, "ATTESTATION_SIGNATURE_VERIFIED"


def evaluate_discovery(manifest: dict[str, Any], signature_b64: str | None = None) -> dict[str, Any]:
    errors = validate_manifest(manifest)
    if errors:
        return {"status": "REJECTED", "eligible_for_proposal": False, "reason": errors[0], "digest": None}
    try:
        validate_source_url(str(manifest["source_url"]))
    except ToolDiscoveryRejected as exc:
        return {"status": "REJECTED", "eligible_for_proposal": False, "reason": str(exc), "digest": None}
    digest = manifest_digest(manifest)
    if manifest.get("content_hash") != digest:
        return {"status": "REJECTED", "eligible_for_proposal": False, "reason": "CONTENT_HASH_MISMATCH", "digest": digest}
    verified, reason = verify_attestation(manifest, signature_b64)
    return {"status": "VERIFIED" if verified else "UNVERIFIED", "eligible_for_proposal": verified, "reason": reason, "digest": digest}


def proposal_eligibility(manifest: dict[str, Any], status: str, verification_reason: str, stored_digest: str) -> dict[str, Any]:
    """Evaluate whether a catalog record may enter a human-reviewed proposal queue."""
    checks: list[dict[str, Any]] = []

    def check(key: str, passed: bool, reason: str) -> None:
        checks.append({"key": key, "passed": passed, "reason": reason})

    digest = manifest_digest(manifest) if isinstance(manifest, dict) else ""
    check("attestation_verified", status == "VERIFIED" and verification_reason == "ATTESTATION_SIGNATURE_VERIFIED", "provider attestation must verify against the configured trust root")
    check("manifest_digest_bound", bool(stored_digest) and stored_digest == digest and manifest.get("content_hash") == digest, "stored and declared manifest digests must match the canonical manifest")
    try:
        validate_source_url(str(manifest.get("source_url", "")))
        source_ok = True
        source_reason = "source is HTTPS and outside blocked local namespaces"
    except ToolDiscoveryRejected as exc:
        source_ok = False
        source_reason = str(exc)
    check("source_allowed", source_ok, source_reason)
    check("risk_bounded", manifest.get("risk_level") in {"low", "medium", "high"}, "critical-risk tools require manual security admission before proposal")
    permissions = manifest.get("permissions") if isinstance(manifest, dict) else None
    dangerous = {"shell", "exec", "filesystem_write", "payment", "purchase", "credential_access", "unbounded_network"}
    elevated = [key for key in dangerous if permissions.get(key) is True] if isinstance(permissions, dict) else list(dangerous)
    check("permissions_bounded", isinstance(permissions, dict) and not elevated, "unbounded execution, credential, payment, or write permissions block automatic proposal eligibility")
    eligible = all(item["passed"] for item in checks)
    return {
        "eligible_for_proposal": eligible,
        "checks": checks,
        "requires_human_ratification": True,
        "execution_authorized": False,
        "note": "Eligibility only permits entry into a human-reviewed proposal queue; it does not reserve funds, purchase, install, or execute the tool.",
    }
