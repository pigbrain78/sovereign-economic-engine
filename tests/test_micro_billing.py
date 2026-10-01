from datetime import datetime, timedelta, timezone

import pytest

from app.micro_billing import (
    EscrowStatus,
    MetricType,
    SplitRevenueContract,
    SplitRule,
    SynapseMicroBillingEngine,
    TelemetryEvent,
)


def event(event_id: str, task_id: str, charge: int) -> TelemetryEvent:
    return TelemetryEvent(event_id, task_id, 'payer', 'provider', MetricType.LLM_OUTPUT_TOKENS, charge, 1, charge, '2026-10-01T00:00:00+00:00')


def contract() -> SplitRevenueContract:
    return SplitRevenueContract('contract-1', (SplitRule('provider', 7000, 'MODEL_PROVIDER'), SplitRule('platform', 3000, 'PLATFORM_FEE')))


def test_escrow_telemetry_settlement_conserves_integer_funds():
    engine = SynapseMicroBillingEngine()
    engine.fund_wallet('payer', 1000)
    hold = engine.create_escrow_hold('task-1', 'payer', 1000)
    engine.record_telemetry(event('event-1', 'task-1', 333))
    engine.record_telemetry(event('event-2', 'task-1', 1))
    record = engine.settle_task('task-1', contract())
    assert hold.status == EscrowStatus.SETTLED
    assert record['payload']['consumed_amount_micro_cents'] == 334
    assert record['payload']['refunded_amount_micro_cents'] + sum(record['payload']['payouts'].values()) == 1000
    assert engine.wallets['payer'] == 666
    assert engine.wallets['provider'] + engine.wallets['platform'] == 334
    assert record['proof_seal'].startswith('synapse:micro_settle:')


def test_duplicate_telemetry_is_idempotent_and_overflow_fails_closed():
    engine = SynapseMicroBillingEngine()
    engine.fund_wallet('payer', 10)
    engine.create_escrow_hold('task-2', 'payer', 10)
    engine.record_telemetry(event('same-event', 'task-2', 6))
    engine.record_telemetry(event('same-event', 'task-2', 6))
    assert engine.escrows['task-2'].consumed_amount_micro_cents == 6
    with pytest.raises(ValueError, match='exceeded'):
        engine.record_telemetry(event('new-event', 'task-2', 5))


def test_expiry_refunds_remaining_balance_only():
    engine = SynapseMicroBillingEngine(escrow_ttl_seconds=60)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    engine.fund_wallet('payer', 100)
    hold = engine.create_escrow_hold('task-3', 'payer', 100, now=start)
    engine.record_telemetry(event('event-3', 'task-3', 25))
    with pytest.raises(ValueError, match='not expired'):
        engine.expire_task('task-3', now=start + timedelta(seconds=59))
    expired = engine.expire_task('task-3', now=start + timedelta(seconds=60))
    assert expired.status == EscrowStatus.EXPIRED
    assert expired.refunded_amount_micro_cents == 75
    assert engine.wallets['payer'] == 75


def test_split_contract_rejects_nonconservation():
    with pytest.raises(ValueError, match='10,000'):
        SplitRevenueContract('bad', (SplitRule('provider', 7000, 'MODEL_PROVIDER'),))
