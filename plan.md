# Sovereign Economic Engine MVP Plan

## Goal

Establish the smallest executable system that makes the economic boundary real: an authorized mission is routed to a qualified model candidate, execution cost is constrained by a wallet, and settlement is auditable.

## Architecture decisions

- Use **FastAPI** for a typed HTTP boundary.
- Use **SQLite** for the first persistence layer.
- Represent monetary values as fixed-precision decimal strings, never binary floats.
- Fail closed when no qualified model satisfies the mission's quality and cost envelope.
- Keep routing, authorization, execution, and ledger concepts separate even while they live in one MVP module.
- Treat the execution endpoint as a provider/executor contract until a real Hugging Face adapter is added.

## Current modules

- Wallet and minimum-liquidity guard.
- Mission economic envelope.
- Qualified model registry.
- Frugal route selection.
- Atomic demo reservation/settlement.
- Append-only economic ledger.
- Corrected substrate contracts in `app/substrate.py`: rootless sandbox command construction, resource metering schema, digest-validated binary checkpoints, WAL-tail replay, deterministic canary routing, and p50/p95/p99 metrics.
- Measured-resource settlement: `ResourceUsage` → `PricingPolicy` → wallet reservation consumption/release, with deterministic settlement IDs and retry-safe replay.
- Synapse micro-billing reference layer: integer micro-cent escrow, telemetry idempotency, expiry refund, conserved revenue splits, and deterministic proof seals.
- Unified FastAPI surface: dollar wallet/routing/settlement, micro-billing escrow, and durable memory endpoints in one service.
- Pocket OS control plane: project context, open loops, advisory Shadow observations, evidence-backed improvement proposals, human decision states, and a verified control-plane event chain.
- Hugging Face model layer: interchangeable candidate catalog, capability/quality/budget selection, provider policy choices, verified-pricing quote contracts, and fail-closed server-side adapter boundary.
- Upgrade treasury: reserve-bucket allocation on deposits, RDP fitness/scenario evaluation gates, Ed25519 human approval, hash-chained upgrade events, development-reserve settlement, and rollback refunds.
- Sandbox promotion: strict candidate bundle validation, deterministic verification receipts, explicit canary activation, atomic active-pointer replacement, and hash-chained rollback/refund records.

## Next phases

1. Split `app/main.py` into domain modules with repositories and services, then replace the demo measurement payload with real provider and candidate-promotion adapters.
2. Add reservation idempotency, leases, fencing tokens, and crash recovery before external execution.
3. Add task profiling and capability matching beyond the current quality/cost filter.
4. Complete the Hugging Face Inference Providers adapter with wallet reservation, measured usage settlement, receipt persistence, live pricing synchronization, and local sandbox fallback.
5. Add outcome verification and economic-memory learning from actual cost and quality.
6. Add governance policies for risk classes, human review, prohibited capabilities, model lifecycle admission, and upgrade promotion/canary controls.
7. Replace the reference file-bundle promoter with a real OS/container sandbox and signed artifact registry before allowing generated code to run.
7. Add a thin operator UI after the API contract stabilizes.
8. Add Pocket OS council evaluation, capability authorization, typed project projections, and live event synchronization; keep Shadow authority at `NONE`.

The micro-billing API is intentionally marked `ready_in_process_memory` until its durable transaction boundary, agent authorization signatures, and MeshLedger adapter are implemented.

The v1.3.2 audit note is therefore incorporated as a tested draft substrate, but its `HOLD / NOT_SEALED` posture remains correct until economic wiring, authenticated governance, and production crash-recovery controls are implemented.

## Non-goals for this first step

- Real external payments.
- Automatic download or admission of arbitrary models.
- Production-grade authentication, multi-tenant isolation, or cryptographic ledger proofs.
