"""Strict deterministic serialization and domain-separated SHA-256 hashing."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


class CanonicalError(ValueError):
    """Raised when a value is not safely JSON-canonicalizable."""


def _validate(value: Any) -> None:
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError("non-finite numbers are not canonicalizable")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate(item)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalError("object keys must be strings")
        for key, item in value.items():
            _validate(key)
            _validate(item)
        return
    raise CanonicalError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return deterministic JSON without coercing unsupported values."""
    _validate(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def digest(value: Any, domain: str) -> str:
    if not domain or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for ch in domain):
        raise CanonicalError(f"invalid domain: {domain!r}")
    raw = domain.encode("utf-8") + b"\x00" + canonical_json(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
