# Forge Blueprint Audit

## Executive finding

`Pasted_content_12.txt` describes a four-stage autonomous software-factory concept:

1. Sense market signals.
2. Generate and mutate code under a budget.
3. Validate and cryptographically seal an artifact.
4. Verify payment and deliver the artifact.

The supplied `forge_master.py` is a simulation/demo, not a production-grade core engine. It does not repair the missing `memory_brain` dependency or package-layout failure in the separate `failure` archive.

## What is useful

The blueprint has a coherent high-level separation between generation, validation, provenance, and distribution. It also reinforces several useful economic-engine ideas:

- explicit per-unit generation budgets;
- a minimum fitness gate;
- local validation before release;
- provenance attached to an artifact;
- payment clearance before delivery.

These concepts could eventually sit above the Sovereign Wallet, Frugal Engine, and micro-billing layers.

## Critical corrections

### 1. HMAC is not secp256k1 signing

`SovereignWallet.sign_agent_provenance` uses an HMAC-SHA256 secret and returns a truncated digest:

```python
hmac_new(self.master_key, payload, hashlib.sha256).hexdigest()[:32]
```

That is a shared-secret MAC, not a public-key signature and not a secp256k1 signature. Buyers or other parties cannot independently verify it without possessing the secret.

Use a real asymmetric signing scheme with a protected key, explicit algorithm identifier, public-key fingerprint, artifact digest, and signature verification path. Do not describe the current HMAC output as a cryptographic provenance signature for external verification.

### 2. The wallet seed is plaintext on disk

The code writes raw entropy to:

```text
./.secure_vault/seed.json
```

with no encryption, file permissions, key wrapping, rotation, backup policy, or tamper detection. The directory name does not make the file secure.

### 3. The fitness score is hard-coded

The validation stage always uses:

```python
passed_tests, total_tests, syntax_warnings = 5, 5, 0
```

Therefore every generated artifact receives a perfect score of `1.0000` and passes the `0.90` gate. This is not evidence of code quality.

The validator must execute real tests in a sandbox, capture test identities and outputs, validate the artifact digest, and make the score reproducible from persisted evidence.

### 4. Generation cost is simulated

The cost is hard-coded to:

```python
Decimal("0.0074")
```

It is not measured from provider token usage, model billing, CPU time, or downstream API charges. It should be represented as an estimate until a trusted ResourceMeter and pricing policy provide actual usage.

### 5. Payment verification is simulated and unsafe

`poll_blockchain_payments()` returns a hard-coded transaction. `verify_and_dispatch()` checks only that:

```python
tx["value_usd"] >= target_price
```

It does not verify:

- chain identity;
- transaction finality or confirmations;
- recipient address;
- asset/token contract;
- token decimals;
- payer identity;
- payment-to-order binding;
- transaction replay;
- already-delivered status;
- exact product SKU binding;
- refunds or under/overpayment policy.

This cannot authorize real delivery.

### 6. Unknown SKU pricing defaults to a price

This line is fail-open:

```python
target_price = self.pricing_table.get(sku, Decimal("9.99"))
```

Unknown products should be rejected, not assigned an arbitrary fallback price.

### 7. Live posting is an external side effect

The blueprint proposes automated posting to Reddit, X, and LinkedIn. Before implementation, each platform needs explicit credential, rate-limit, consent, moderation, and idempotency controls. A transaction callback must not automatically publish broad public messages without a policy gate.

### 8. Provenance is incomplete

The current payload signs `mutant_id + code_bytes`, but does not bind:

- validator version;
- test evidence digest;
- dependency lockfile;
- build environment;
- source inputs;
- pricing and budget decision;
- product SKU;
- release version;
- signer public key;
- artifact hash in a structured envelope.

A seal should identify exactly what was approved and under which policy.

## Recommended safe architecture

```text
Market signal
    ↓
Task / product proposal
    ↓
Governance + budget estimate
    ↓
Micro-escrow / wallet reservation
    ↓
Generation
    ↓
Trusted sandbox execution
    ↓
Measured ResourceUsage
    ↓
Quality validator
    ↓
Artifact digest + signed provenance envelope
    ↓
Human or governed release approval
    ↓
Payment watcher
    ↓
Verified payment event
    ↓
Idempotent delivery
    ↓
Economic ledger
```

The current Sovereign Economic Engine already provides early pieces of this design: wallet envelopes, measured settlement, resource pricing, idempotent execution IDs, and micro-cent escrow primitives.

## Safe MVP boundary

A safe next prototype should:

1. generate a local artifact;
2. run deterministic tests inside a local sandbox;
3. calculate a real fitness result from test evidence;
4. hash the artifact and evidence;
5. sign a structured provenance record using an actual asymmetric key;
6. store a draft release record;
7. simulate payment using an explicitly marked test fixture;
8. require a local approval step before any external publication or delivery.

Do not connect real payment watchers or public posting until recipient binding, replay protection, finality, credentials, and idempotent delivery are implemented.

## Status

**Blueprint only / not production verified.** The simulated budget, validator, wallet signature, payment event, and public dispatch must not be treated as evidence of real profit, real cryptographic provenance, or real payment clearance.
