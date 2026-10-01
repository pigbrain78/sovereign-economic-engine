from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class MetricType(str, Enum):
    LLM_INPUT_TOKENS = "LLM_INPUT_TOKENS"
    LLM_OUTPUT_TOKENS = "LLM_OUTPUT_TOKENS"
    TOOL_INVOCATION = "TOOL_INVOCATION"
    SANDBOX_CPU_SEC = "SANDBOX_CPU_SEC"
    STORAGE_BYTE_HOURS = "STORAGE_BYTE_HOURS"


class EscrowStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SETTLED = "SETTLED"
    EXPIRED = "EXPIRED"
    FORCE_REFUNDED = "FORCE_REFUNDED"


@dataclass(frozen=True)
class TelemetryEvent:
    event_id: str
    task_id: str
    payer_agent_id: str
    provider_agent_id: str
    metric_type: MetricType
    quantity: int
    unit_price_micro_cents: int
    total_charge_micro_cents: int
    timestamp: str
    signature: str | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.task_id or not self.payer_agent_id or not self.provider_agent_id:
            raise ValueError("event and actor identifiers are required")
        if self.quantity < 1 or self.unit_price_micro_cents < 0 or self.total_charge_micro_cents < 0:
            raise ValueError("telemetry quantities and prices must be non-negative")
        expected = self.quantity * self.unit_price_micro_cents
        if self.total_charge_micro_cents != expected:
            raise ValueError(f"telemetry charge mismatch: expected {expected}, got {self.total_charge_micro_cents}")

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "payer_agent_id": self.payer_agent_id,
            "provider_agent_id": self.provider_agent_id,
            "metric_type": self.metric_type.value,
            "quantity": self.quantity,
            "unit_price_micro_cents": self.unit_price_micro_cents,
            "total_charge_micro_cents": self.total_charge_micro_cents,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class SplitRule:
    beneficiary_id: str
    basis_points: int
    role: str

    def __post_init__(self) -> None:
        if not self.beneficiary_id or not 0 <= self.basis_points <= 10_000:
            raise ValueError("invalid split rule")


@dataclass(frozen=True)
class SplitRevenueContract:
    contract_id: str
    rules: tuple[SplitRule, ...]

    def __post_init__(self) -> None:
        if not self.contract_id or not self.rules or sum(rule.basis_points for rule in self.rules) != 10_000:
            raise ValueError("split rules must sum to exactly 10,000 basis points")
        if len({rule.beneficiary_id for rule in self.rules}) != len(self.rules):
            raise ValueError("beneficiaries must be unique")


@dataclass
class MicroEscrowHold:
    hold_id: str
    task_id: str
    payer_agent_id: str
    locked_amount_micro_cents: int
    created_at: datetime
    expires_at: datetime
    consumed_amount_micro_cents: int = 0
    refunded_amount_micro_cents: int = 0
    status: EscrowStatus = EscrowStatus.ACTIVE
    event_ids: set[str] = field(default_factory=set)


class SynapseMicroBillingEngine:
    """In-memory reference engine; production use requires durable transactional storage."""

    def __init__(self, escrow_ttl_seconds: int = 3600):
        if escrow_ttl_seconds <= 0:
            raise ValueError("escrow TTL must be positive")
        self.escrow_ttl_seconds = escrow_ttl_seconds
        self.wallets: dict[str, int] = {}
        self.escrows: dict[str, MicroEscrowHold] = {}
        self.settlement_ledger: list[dict[str, Any]] = []
        self.events: dict[str, TelemetryEvent] = {}

    def fund_wallet(self, agent_id: str, amount_micro_cents: int) -> int:
        self._nonnegative(amount_micro_cents)
        if not agent_id:
            raise ValueError("agent_id is required")
        self.wallets[agent_id] = self.wallets.get(agent_id, 0) + amount_micro_cents
        return self.wallets[agent_id]

    def create_escrow_hold(self, task_id: str, payer_agent_id: str, lock_amount_micro_cents: int, *, now: datetime | None = None) -> MicroEscrowHold:
        self._nonnegative(lock_amount_micro_cents)
        if not task_id or task_id in self.escrows:
            raise ValueError("task_id must be unique")
        balance = self.wallets.get(payer_agent_id, 0)
        if balance < lock_amount_micro_cents:
            raise ValueError(f"insufficient balance: {balance} < {lock_amount_micro_cents}")
        current = now or datetime.now(timezone.utc)
        hold = MicroEscrowHold(f"hold_{secrets.token_hex(8)}", task_id, payer_agent_id, lock_amount_micro_cents, current, current + timedelta(seconds=self.escrow_ttl_seconds))
        self.wallets[payer_agent_id] -= lock_amount_micro_cents
        self.escrows[task_id] = hold
        return hold

    def record_telemetry(self, event: TelemetryEvent) -> MicroEscrowHold:
        hold = self._active_hold(event.task_id)
        if event.payer_agent_id != hold.payer_agent_id:
            raise ValueError("telemetry payer does not match escrow payer")
        if event.event_id in hold.event_ids:
            return hold
        if event.event_id in self.events:
            raise ValueError("event_id already belongs to another task")
        if hold.consumed_amount_micro_cents + event.total_charge_micro_cents > hold.locked_amount_micro_cents:
            raise ValueError("escrow balance exceeded; execution halted")
        hold.consumed_amount_micro_cents += event.total_charge_micro_cents
        hold.event_ids.add(event.event_id)
        self.events[event.event_id] = event
        return hold

    def settle_task(self, task_id: str, contract: SplitRevenueContract, *, now: datetime | None = None) -> dict[str, Any]:
        hold = self._active_hold(task_id)
        current = now or datetime.now(timezone.utc)
        refund = hold.locked_amount_micro_cents - hold.consumed_amount_micro_cents
        payouts = self._split(hold.consumed_amount_micro_cents, contract.rules)
        self.wallets[hold.payer_agent_id] = self.wallets.get(hold.payer_agent_id, 0) + refund
        for beneficiary_id, payout in payouts.items():
            self.wallets[beneficiary_id] = self.wallets.get(beneficiary_id, 0) + payout
        hold.refunded_amount_micro_cents = refund
        hold.status = EscrowStatus.SETTLED
        payload = {
            "contract_id": contract.contract_id,
            "task_id": task_id,
            "hold_id": hold.hold_id,
            "payer_agent_id": hold.payer_agent_id,
            "consumed_amount_micro_cents": hold.consumed_amount_micro_cents,
            "refunded_amount_micro_cents": refund,
            "payouts": payouts,
            "timestamp": current.isoformat(),
        }
        seal = self._seal(payload)
        record = {"proof_seal": seal, "payload": payload}
        self.settlement_ledger.append(record)
        return record

    def expire_task(self, task_id: str, *, now: datetime | None = None) -> MicroEscrowHold:
        hold = self._active_hold(task_id)
        current = now or datetime.now(timezone.utc)
        if current < hold.expires_at:
            raise ValueError("escrow has not expired")
        refund = hold.locked_amount_micro_cents - hold.consumed_amount_micro_cents
        self.wallets[hold.payer_agent_id] = self.wallets.get(hold.payer_agent_id, 0) + refund
        hold.refunded_amount_micro_cents = refund
        hold.status = EscrowStatus.EXPIRED
        return hold

    def _active_hold(self, task_id: str) -> MicroEscrowHold:
        hold = self.escrows.get(task_id)
        if not hold or hold.status != EscrowStatus.ACTIVE:
            raise ValueError(f"no active escrow for task {task_id}")
        return hold

    @staticmethod
    def _nonnegative(amount: int) -> None:
        if amount < 0:
            raise ValueError("micro-cent amounts cannot be negative")

    @staticmethod
    def _split(amount: int, rules: tuple[SplitRule, ...]) -> dict[str, int]:
        payouts: dict[str, int] = {}
        remaining = amount
        for index, rule in enumerate(rules):
            if index == len(rules) - 1:
                payout = remaining
            else:
                payout = amount * rule.basis_points // 10_000
                remaining -= payout
            payouts[rule.beneficiary_id] = payout
        if sum(payouts.values()) != amount:
            raise AssertionError("split conservation violated")
        return payouts

    @staticmethod
    def _seal(payload: dict[str, Any]) -> str:
        # The payload is restricted to strings, integers, dicts, and stable lists,
        # so sorted compact JSON provides deterministic canonical bytes here.
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return f"synapse:micro_settle:{hashlib.sha256(canonical).hexdigest()}"
