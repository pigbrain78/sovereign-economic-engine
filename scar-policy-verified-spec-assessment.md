# Scar Policy Verified Specification — Evidence Assessment

## What the note claims

`Pasted_content_16.txt` describes a `sail.failure` implementation with:

- governed Scar → Policy conversion;
- deterministic SHA-256 policy hashes;
- hash-chained append-only persistence;
- distinct BLOCK, QUARANTINE, WARN, and AUDIT outcomes;
- enforcement for all five declared rule types;
- JUnit XML-based authoritative test receipts;
- 40 committed tests passing.

It explicitly claims that earlier gaps were corrected:

- `QUARANTINE` now returns `allowed=False` and populates `quarantined_by`;
- `AUDIT` remains non-blocking but sets `audited=True`;
- `REQUIRED_PRECONDITION`, `MAX_LATENCY_CAP`, and `PARAM_ASSERTION` are enforced;
- harness counts come from JUnit XML and duration uses `time.monotonic()`.

If these behaviors exist in the referenced source, they address the defects identified in the previous review.

## What is verified here

Nothing from the claimed 40-test implementation is independently executed in this workspace. The attachment is a formatted specification/review document. It does not include:

- `sail/failure/policy.py`;
- `sail/failure/persistence.py`;
- `sail/failure/harness.py`;
- the five named test modules;
- package metadata or dependencies;
- the referenced runtime/build artifact.

The statement `python3 -m pytest tests/ → 40 passed, 0 failed` is therefore a claim from the document, not a locally reproduced result.

## Relationship to earlier package failure

This note appears to describe a different or newer `sail.failure` source tree. It does not itself provide the missing top-level `memory_brain` module, install metadata, or the scar-score implementation that previously returned values inconsistent with its tests.

It may resolve the package namespace and policy-layer gaps if its actual source is supplied, but that cannot be established from this text alone.

## Acceptance checks when source is available

1. Run the exact 40-test suite in a clean environment.
2. Confirm `QUARANTINE` is isolated and cannot execute through a generic `allowed` branch.
3. Confirm `AUDIT` is observable through a typed result field.
4. Exercise each of the five rule types with both violating and clean payloads.
5. Mutate payloads, delete middle ledger entries, truncate the ledger, and verify boot rejection.
6. Confirm JUnit counts and duration are parsed from the report rather than console substrings.
7. Confirm no second canonicalization implementation or unsafe fallback exists.
8. Confirm package installation resolves `sail.failure` and all declared dependencies.
9. Reconcile or remove the earlier scar-score tests before release.

## Status

**Specification improved; implementation unverified.** The note provides a credible target contract and claims 40/40 tests, but no source or test artifact was attached to substantiate those claims.
