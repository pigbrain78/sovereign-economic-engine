# SyncTrace PocketSRE

One phone-sized console for two jobs:

- **SyncTrace**: diagnose local-first sync. Inspect on-device SQLite, record replication events, and run chaos scenarios (network drop, delayed ack, split-brain writes) against a sync engine.
- **PocketSRE**: on-call triage. Ingest alerts, rank them, and run approved runbook steps with an audit trail.

The shared core is a hash-chained event log. Every chaos run and every runbook step is recorded, so a session can be replayed and verified.

## Layout
- `synctrace/chaos.py`: deterministic chaos scenarios over a replication log.
- `synctrace/inspect.py`: read-only SQLite inspection.
- `pocketsre/triage.py`: alert ranking.
- `pocketsre/runbook.py`: runbook steps gated by explicit approval.
- `core/ledger.py`: hash-chained event log shared by both.

## Non-goals
- No automatic remediation. Runbook steps need an approval record.
- No execution of downloaded code.
