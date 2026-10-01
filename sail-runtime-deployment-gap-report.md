# SAIL Runtime Deployment Notes — Gap Report

## What this note establishes

`Pasted_content_14.txt` defines a stronger deployment contract for a versioned FastAPI SAIL runtime:

- Ed25519 signing through PyNaCl is mandatory.
- `rfc8785` is intended to be the single canonicalization authority.
- Governor signing material must come from a secret manager or environment, not source.
- An append-only JSONL ledger must verify hash links and signatures at boot.
- TrustPolicy must reject tools outside an explicit allowlist.
- Execution requires a real, unconsumed Governor-issued verdict bound to the proposal.
- Verdict replay, cross-proposal references, and invented hashes must be rejected.

These requirements directly improve the earlier failure-package weaknesses around the development HMAC secret, unsafe canonicalization fallback, and missing execution barrier.

## What it does not resolve

### Scar-score mismatch remains open

The note specifies runtime deployment, but it does not define the scoring formula that would reconcile the observed test expectations (`60.75`, `43.3333`) with the implementation results (`57.75`, `45.0`). The score contract still requires an authoritative formula or updated tests.

### The authoritative artifact is missing

The note references:

```text
sail_foundation_with_runtime.zip
BUILD_MANIFEST.json v1.1.2
```

Neither artifact is present in the uploaded attachment. The runtime implementation, ten test suites, source manifest, and PDF specification cannot be verified from deployment notes alone.

### The referenced application surface is unavailable

The startup command refers to:

```text
app:build_app_factory
```

but the supplied failure archive contains no `app.py`, `build_app_factory`, `SAILApp`, `TrustPolicy`, Ed25519 ledger implementation, or `/api/v1` routes. This is a contract description, not proof that those components exist.

### Dependency declarations are instructions, not evidence

The note lists:

```text
fastapi uvicorn pynacl pydantic rfc8785 httpx
```

but no lockfile, version pins, package metadata, or reproducible build file is included.

### Key rotation is acknowledged but not implemented here

The note correctly states that changing the governor key breaks verification of historical signatures. A production implementation still needs a versioned key registry or explicit migration protocol; simply verifying the ledger with the current key is insufficient for rotation.

### Ledger durability remains unverified

Boot verification of a JSONL hash/signature chain is useful, but the note does not demonstrate cross-process locking, atomic append and `fsync`, partial-line recovery, backup restoration, or concurrent verdict/execute behavior.

## Recommended acceptance gate

Do not mark the runtime sealed until the referenced source artifact is available and the following are verified:

1. Clean installation from declared, pinned dependencies.
2. `/api/v1/health` reports `ledger_ready: true` on a fresh ledger.
3. Tampered payloads, hash links, signatures, and partial records fail closed at boot.
4. Unknown tools are rejected before execution.
5. Verdicts are Ed25519-verified, proposal-bound, single-use, and replay-resistant.
6. Missing governor secret prevents production startup.
7. Canonicalization has exactly one implementation and no `default=str` fallback.
8. Key rotation preserves historical verification through an explicit migration path.
9. The scar-score formula and tests agree.
10. Full test-suite counts are reproduced from the supplied build artifact.

## Status

**Improved specification; implementation unverified.** This note is a useful security/deployment contract, but it does not itself repair the uploaded package or prove that the referenced runtime exists.
