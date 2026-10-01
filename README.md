# Sovereign Economic Engine

A runnable backend for governed, cost-aware AI work. The service combines the verified pieces developed in this project:

- **Economic wallet**: capital, reservations, minimum-liquidity protection, measured settlement, refunds, and an append-only ledger.
- **Frugal model routing**: task-type, quality, reliability, and budget filtering with fail-closed `HOLD` decisions.
- **Resource pricing**: CPU, memory, I/O, token, and wall-time measurements converted to cost by a separate pricing policy.
- **Synapse micro-billing**: integer micro-cent wallets, escrow, telemetry, expiration refunds, revenue splits, and deterministic settlement seals.
- **Durable memory**: deduplicated memory records with a verifiable SQLite hash-chained provenance ledger.
- **Substrate utilities**: sandbox command construction, checkpoint validation, WAL-tail replay, deterministic canary routing, and latency metrics.

The unverified external SAIL/DRQuinn packages are intentionally not in the execution path.

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

## Memory API

- `POST /memory/remember`
- `POST /memory/search`
- `GET /memory/{memory_id}`
- `GET /memory/{memory_id}/explain`
- `POST /memory/{memory_id}/retract`
- `GET /memory/health`

Memory is informational only. It does not authorize execution.

## Validation

```bash
cd /home/ubuntu/sovereign-economic-engine
. .venv/bin/activate
python3 -m compileall -q app tests
pytest -q
```

Current verified result: **12 passed**.

## Production boundary

This is a functioning integrated MVP, not a sealed production platform. Remaining work includes a trusted provider executor, direct resource measurement, durable micro-billing persistence, authentication and tenant isolation, Ed25519 capability authorization, strict RFC 8785/JCS integration, crash-safe multi-process settlement, and a governed sandbox promotion path for generated code.

Core rule:

> An autonomous system may consume resources only inside an explicitly authorized economic envelope, and every material consumption must produce attributable, auditable evidence.
