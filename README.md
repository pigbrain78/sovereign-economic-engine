# Sovereign Economic Engine

A runnable backend for governed, cost-aware AI work. The service combines the verified pieces developed in this project:

- **Economic wallet**: capital, reservations, minimum-liquidity protection, measured settlement, refunds, and an append-only ledger.
- **Frugal model routing**: task-type, quality, reliability, and budget filtering with fail-closed `HOLD` decisions.
- **Resource pricing**: CPU, memory, I/O, token, and wall-time measurements converted to cost by a separate pricing policy.
- **Synapse micro-billing**: integer micro-cent wallets, escrow, telemetry, expiration refunds, revenue splits, and deterministic settlement seals.
- **Durable memory**: deduplicated memory records with a verifiable SQLite hash-chained provenance ledger.
- **Pocket OS control plane**: projects, open improvement loops, advisory AI Shadow observations, evidence-backed proposals, human decision records, and a separate hash-chained control-plane event log.
- **Hugging Face model layer**: interchangeable Inference Provider catalog, `cheapest`/`fastest`/`preferred` routing policies, verified-pricing quotes, and a server-side adapter that fails closed until provider credentials and wallet settlement are configured.
- **Upgrade treasury**: every deposit can split into operating, development, safety, and owner reserves; RDP fitness and scenario evidence gate upgrades; Ed25519 approval signatures authorize development-reserve holds; verified completion settles and failed completion refunds.
- **Substrate utilities**: sandbox command construction, checkpoint validation, WAL-tail replay, deterministic canary routing, and latency metrics.

The unverified external SAIL/DRQuinn packages are intentionally not in the execution path.

## Pocket OS control plane

Pocket OS provides the cognitive and governance surface around the economic kernel:

```text
Shadow observes
    ↓
Builder proposes with evidence
    ↓
Human reviews or requests more testing
    ↓
Economic Engine may later reserve funds
    ↓
Provider execution settles through the wallet
```

Pocket OS routes:

- `POST /pocket/projects`
- `GET /pocket/projects`
- `POST /pocket/projects/{project_id}/loops`
- `POST /pocket/projects/{project_id}/shadow`
- `GET /pocket/shadow`
- `POST /pocket/proposals`
- `GET /pocket/proposals`
- `POST /pocket/proposals/{proposal_id}/decision`
- `GET /pocket/events`

Shadow responses explicitly report:

```text
AUTHORITY: NONE
CAN EXECUTE: NO
CAN RATIFY: NO
```

Purchase recommendations require evidence and enter `HOLD_FOR_APPROVAL`. A human decision can move a proposal to `USER_APPROVED_PENDING_EXTERNAL_PURCHASE`, `REJECTED`, or `TRIAL_EXTENDED`, but it does not purchase, reserve wallet funds, or set `execution_authorized` to true. This preserves the constitutional boundary:

> Memory may inform execution. Memory may not authorize execution.

## Run it

```bash
cd /home/ubuntu/sovereign-economic-engine
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open the interactive API at `http://localhost:8000/docs`.

SQLite files are created at `sovereign.db` and `sovereign-memory.db`. Override them with `SOVEREIGN_DB` and `SOVEREIGN_MEMORY_DB`.

## Core economic flow

```text
Create wallet
    ↓
Register qualified models
    ↓
Create mission with task type, quality floor, and max cost
    ↓
Ask the Frugal Engine for a route
    ↓
Run an external/provider executor
    ↓
Submit measured resource usage
    ↓
PricingPolicy calculates actual cost
    ↓
Wallet settles actual cost and releases unused reservation
    ↓
Ledger records the evidence
```

The current service does not execute a model provider itself. `/executions` is the boundary where a future trusted provider adapter will submit measurements.

## Local executor boundary

Captured `local_only` envelopes can be sent to `POST /capture/{envelope_id}/execute-local` only when an operator configures `LOCAL_EXECUTOR_COMMAND_JSON` as a JSON string array, for example:

```bash
export LOCAL_EXECUTOR_COMMAND_JSON='["python3","-c","import sys; print(sys.stdin.read())"]'
```

The engine reserves the envelope maximum before starting the command, passes the captured thought over stdin, measures wall time and I/O, prices the measured result, then settles the actual amount or refunds the reservation. Reusing an `execution_id` returns the prior receipt without running the command again. Non-local privacy modes are rejected, and an unconfigured executor fails closed at `503`.

This adapter is a **process boundary only**, not a security sandbox. It does not provide VM/container isolation, model weights, or a trusted code-execution boundary. A production local-AI runtime still needs a hardened sandbox, resource enforcement, authentication, and crash-safe reservation recovery.

### Provider-neutral local model layer

`app/local_model.py` provides one model-selection contract for local runtimes. The current adapters are:

- `local-command`: wraps the explicit `LOCAL_EXECUTOR_COMMAND_JSON` process adapter.
- `Ollama`: optional local HTTP inference through `LOCAL_OLLAMA_MODEL` and `LOCAL_OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`).

Inspect available local models with `GET /local-models`. Pass a specific `model_id` to the local execution request when more than one adapter is configured. If no local adapter is available, selection fails closed; the system never silently falls back to Hugging Face or another cloud provider.

### Rootless container sandbox

`app/sandbox_model.py` adds a separate `rootless_container` adapter. It builds a Podman command with a non-root UID, read-only root filesystem, `no-new-privileges`, all capabilities dropped, disabled network, bounded memory/PIDs, and a bounded temporary filesystem. It requires explicit `LOCAL_SANDBOX_IMAGE` and `LOCAL_SANDBOX_COMMAND_JSON` configuration. Podman is installed in the validation sandbox, but its subordinate UID/GID mapping prevents unpacking a normal image here; therefore a real container run is **not available in this environment**, and selection fails closed rather than silently using the unsandboxed command adapter.

### Merged cryptographic provenance

The security primitives adapted from `software-defined-reflex-agent` now live under `app/crypto_provenance/`:

- strict canonical JSON rejects non-finite numbers and unsupported values;
- domain-separated SHA-256 digests bind AgentRun provenance to the Sovereign namespace;
- optional Ed25519 signatures are enabled only when `SOVEREIGN_SIGNING_MODE=ed25519` and valid Base64 keys are supplied;
- the imported SQLite ledger implementation remains available as a reusable append-only ledger primitive.

AgentRuns expose their `input_hash`, `output_hash`, `provenance_hash`, signing algorithm, and optional signature through `GET /agent-runs/{execution_id}`. Hash-only mode remains the default development behavior; missing or malformed Ed25519 material fails closed rather than producing an unverified signature.

### Merged Pocket OS memory brain

The Pocket OS Python `memory_brain` package is now included under `app/pocket_memory/` and is authoritative behind `app/pocket_memory_adapter.py`. The existing `/memory/*` API remains compatible, while writes now use Pocket OS's ledger-first commit path with exact deduplication, immutable history, provenance, review-gated irreversible operations, and contradiction tracking. New projections are available at `POST /memory/context` for grounded context packages and `GET /memory/contradictions` for unresolved contradictions. Memory informs agents; it does not authorize execution.

## Dollar-wallet API sequence

1. `POST /wallets`
2. `POST /models`
3. `POST /missions`
4. `POST /missions/{mission_id}/decide`
5. `POST /executions`
6. `GET /wallets/{wallet_id}`
7. `GET /ledger/{wallet_id}`

Example wallet:

```json
{
  "capital": "10.00",
  "minimum_liquidity": "2.00"
}
```

Example mission:

```json
{
  "wallet_id": "wallet_...",
  "description": "Research a company",
  "max_cost": "1.00",
  "minimum_quality": 0.85,
  "task_type": "research"
}
```

A routing response is either:

```json
{
  "status": "APPROVED",
  "selected_model": "model_..."
}
```

or:

```json
{
  "status": "HOLD",
  "reason": "NO_QUALIFIED_MODEL_WITHIN_ECONOMIC_ENVELOPE"
}
```

An execution submits measured usage, not a caller-selected final price:

```json
{
  "mission_id": "mission_...",
  "model_id": "model_...",
  "execution_id": "exec-001",
  "cpu_seconds": 0.38,
  "peak_memory_bytes": 149946368,
  "io_read_bytes": 12000,
  "io_write_bytes": 6240,
  "token_count": 1540,
  "wall_seconds": 0.812,
  "outcome": "verified_success",
  "evidence": {"validator": "deterministic-checker"}
}
```

Repeating the same `execution_id` returns the existing settlement and does not charge the wallet again.

## Micro-billing API

Micro-billing uses integer micro-cents:

```text
1 micro-cent = $0.000001
1,000,000 micro-cents = $1.00
```

Sequence:

```text
POST /micro/wallets/{agent_id}/fund
POST /micro/escrows
POST /micro/telemetry
POST /micro/escrows/{task_id}/settle
```

Example fund:

```json
{"amount_micro_cents": 1000}
```

Example telemetry:

```json
{
  "event_id": "event-1",
  "task_id": "task-1",
  "payer_agent_id": "payer",
  "provider_agent_id": "provider",
  "metric_type": "LLM_OUTPUT_TOKENS",
  "quantity": 25,
  "unit_price_micro_cents": 2,
  "total_charge_micro_cents": 50,
  "timestamp": "2026-10-01T00:00:00+00:00"
}
```

The engine rejects charge mismatches, duplicate event IDs, payer mismatches, and escrow overflow. Settlement enforces:

```text
sum(payouts) + refund = original escrow
```

The current micro-billing engine is process-memory state. It is a tested reference layer, not yet a durable multi-process payment ledger.

## Upgrade treasury

Wallet creation defaults to a 10% development reserve, 5% safety reserve, and 5% owner reserve. The remainder is operating capital. Rates are explicit basis-point fields and can be changed only by the wallet owner or a future governed policy—not by an agent.

Upgrade routes:

- `POST /wallets/{wallet_id}/deposit`
- `POST /upgrades/proposals`
- `GET /upgrades/proposals`
- `POST /upgrades/proposals/{proposal_id}/evaluate`
- `POST /upgrades/proposals/{proposal_id}/approve`
- `POST /upgrades/proposals/{proposal_id}/complete`
- `GET /upgrades/events`
- `POST /upgrades/proposals/{proposal_id}/promote`
- `POST /upgrades/proposals/{proposal_id}/activate`
- `POST /upgrades/proposals/{proposal_id}/rollback`
- `GET /upgrades/proposals/{proposal_id}/promotion`

The lifecycle is:

```text
PENDING_EVALUATION → PENDING_HUMAN_GATE → FUNDS_RESERVED → SETTLED
       │                     │                    │
       └── EVALUATION_FAILED └── signature gate   └── ROLLED_BACK + refund
```

`TREASURY_APPROVAL_PUBLIC_KEY_B64` must contain the server-side Ed25519 public key. Approval signatures are verified against a canonical payload containing the proposal, wallet, amount, and actor. The upgrade ledger is independently hash-chained and included in `/health`.

## Sandbox promotion and rollback

The promotion layer accepts a candidate file bundle only after funds are reserved. It rejects absolute paths, traversal segments, unsupported file types, oversized files, duplicate candidate IDs, invalid Python syntax, and invalid JSON. Accepted candidates are staged below `SOVEREIGN_SANDBOX_ROOT` and receive a deterministic verification receipt containing the candidate hash, file count, byte count, and checks performed.

Activation requires an explicit canary result. A passed canary atomically replaces the active candidate pointer with `os.replace`; a failed canary leaves the candidate inactive. Rollback verifies the active candidate hash, restores the previous pointer when available, and refunds the reserved development amount. Promotion and rollback events are appended to the upgrade hash chain.

This is a safe reference promotion layer, not a container or VM boundary. It does not execute candidate code, install dependencies, or claim process isolation. Production deployment still requires a separate OS/container sandbox, signed artifact registry, crash-safe fencing, and an application-specific health/canary executor.

## Memory API

- `POST /memory/remember`
- `POST /memory/search`
- `GET /memory/{memory_id}`
- `GET /memory/{memory_id}/explain`
- `POST /memory/{memory_id}/retract`
- `GET /memory/health`

Memory is informational only. It does not authorize execution.

## First hero workflow: thought capture

`POST /capture` turns a text thought into a durable memory record, a planned mission, and a bounded execution envelope. The request requires a wallet, privacy mode (`local_only`, `user_server`, or `approved_external`), task type, maximum cost, capabilities, and minimum quality. The response includes a deterministic quote hash and idempotency key.

Capture is deliberately non-executing: it does not call a model, reserve funds, or authorize a provider. It returns `execution_authorized: false`, `escrow_reserved: false`, and `AWAITING_EXECUTION` until a governed executor is connected. Repeating the same capture is idempotent and does not create another mission or charge.

The Pocket OS frontend exposes this at `/capture` as **Thought Capture**. The user can enter a project-improvement thought, choose privacy residency, set a maximum budget, and inspect the resulting memory, mission, envelope, quote hash, and expiry.

## Hugging Face model layer

The Hugging Face layer keeps models interchangeable while preserving the economic boundary:

```text
Catalog → capability filter → quality floor → budget quote → wallet reservation → provider call → measured settlement
```

Routes:

- `GET /hf/models`
- `GET /hf/provider-status`
- `POST /hf/quote`
- `POST /hf/chat`

The catalog contains candidate model IDs, capabilities, context windows, quality/reliability estimates, provider options, and pricing state. `POST /hf/quote` can select by `cheapest`, `fastest`, or `preferred` policy, but it always returns `execution_authorized: false` and `wallet_reservation_required: true`.

Production pricing must be supplied through the server-only `HF_MODEL_PRICING_JSON` environment variable and verified against the active provider. Without verified rates, model selection fails closed. Provider calls also require `HF_TOKEN` and are disabled unless the explicit `HF_ALLOW_UNSETTLED_CHAT=true` integration flag is set; this flag is a temporary adapter-probe boundary, not a substitute for wallet reservation and settlement.

The frontend never receives `HF_TOKEN`. Hugging Face's Inference Providers documentation supports model suffix policies such as `:cheapest`, `:fastest`, and `:preferred`; the adapter uses the OpenAI-compatible router endpoint for chat tasks. See [Inference Providers](https://huggingface.co/docs/inference-providers/en/index), [Pricing and Billing](https://huggingface.co/docs/inference-providers/en/pricing), and [Inference Endpoints](https://huggingface.co/docs/inference-endpoints/en/index).

## Validation

```bash
cd /home/ubuntu/sovereign-economic-engine
. .venv/bin/activate
python3 -m compileall -q app tests
pytest -q
```

Current verified result: **48 passed**.

## Production boundary

This is a functioning integrated MVP, not a sealed production platform. Remaining work includes promotion sandbox integration for candidate code, multi-process locking/fencing for upgrade settlement, live provider pricing synchronization, trusted provider execution, direct resource measurement, durable micro-billing persistence, authentication and tenant isolation, strict RFC 8785/JCS integration, and a real Pocket OS council/capability backend beyond the current explicit human-decision record.

Core rule:

> An autonomous system may consume resources only inside an explicitly authorized economic envelope, and every material consumption must produce attributable, auditable evidence.
