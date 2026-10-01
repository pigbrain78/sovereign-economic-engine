from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from app.substrate import PricingPolicy, ResourceUsage
from app.memory import MemoryAPI
from app.micro_billing import MetricType, SplitRevenueContract, SplitRule, SynapseMicroBillingEngine, TelemetryEvent

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(__import__('os').environ.get('SOVEREIGN_DB', BASE_DIR / 'sovereign.db'))

app = FastAPI(title='Sovereign Economic Engine', version='0.1.0')
MICRO_BILLING = SynapseMicroBillingEngine()
MEMORY = MemoryAPI(__import__('os').environ.get('SOVEREIGN_MEMORY_DB', BASE_DIR / 'sovereign-memory.db'))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def money(value: str | Decimal | int | float) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(Decimal('0.000001'))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('invalid monetary value') from exc
    if result < 0:
        raise ValueError('monetary values cannot be negative')
    return result


def money_str(value: Decimal | str | int | float) -> str:
    return f'{money(value):.6f}'


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def init_db() -> None:
    with closing(connect()) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS wallets (
            id TEXT PRIMARY KEY,
            total TEXT NOT NULL,
            available TEXT NOT NULL,
            reserved TEXT NOT NULL,
            spent TEXT NOT NULL,
            minimum_liquidity TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS models (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            task_types TEXT NOT NULL,
            quality REAL NOT NULL,
            reliability REAL NOT NULL,
            cost_per_task TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS missions (
            id TEXT PRIMARY KEY,
            wallet_id TEXT NOT NULL REFERENCES wallets(id),
            description TEXT NOT NULL,
            max_cost TEXT NOT NULL,
            minimum_quality REAL NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'general',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reservations (
            id TEXT PRIMARY KEY,
            wallet_id TEXT NOT NULL REFERENCES wallets(id),
            mission_id TEXT NOT NULL REFERENCES missions(id),
            amount TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            settled_at TEXT
        );
        CREATE TABLE IF NOT EXISTS routing_decisions (
            id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions(id),
            selected_model_id TEXT,
            decision TEXT NOT NULL,
            alternatives TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id TEXT PRIMARY KEY,
            wallet_id TEXT NOT NULL REFERENCES wallets(id),
            mission_id TEXT REFERENCES missions(id),
            reservation_id TEXT REFERENCES reservations(id),
            event TEXT NOT NULL,
            amount TEXT NOT NULL,
            metadata TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS executions (
            execution_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions(id),
            model_id TEXT NOT NULL REFERENCES models(id),
            reservation_id TEXT NOT NULL REFERENCES reservations(id),
            usage TEXT NOT NULL,
            outcome TEXT NOT NULL,
            evidence TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settlements (
            settlement_id TEXT PRIMARY KEY,
            execution_id TEXT NOT NULL UNIQUE REFERENCES executions(execution_id),
            wallet_id TEXT NOT NULL REFERENCES wallets(id),
            amount TEXT NOT NULL,
            previous_event_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        ''')
        mission_columns = {row['name'] for row in db.execute('PRAGMA table_info(missions)').fetchall()}
        if 'task_type' not in mission_columns:
            db.execute("ALTER TABLE missions ADD COLUMN task_type TEXT NOT NULL DEFAULT 'general'")
        db.commit()


@app.on_event('startup')
def startup() -> None:
    init_db()


class WalletCreate(BaseModel):
    capital: str = Field(pattern=r'^\d+(\.\d{1,6})?$')
    minimum_liquidity: str = Field(default='0.000000', pattern=r'^\d+(\.\d{1,6})?$')


class ModelCreate(BaseModel):
    id: str | None = None
    name: str
    task_types: list[str] = Field(min_length=1)
    quality: float = Field(ge=0, le=1)
    reliability: float = Field(ge=0, le=1)
    cost_per_task: str = Field(pattern=r'^\d+(\.\d{1,6})?$')
    latency_ms: int = Field(gt=0)


class MissionCreate(BaseModel):
    wallet_id: str
    description: str = Field(min_length=1)
    max_cost: str = Field(pattern=r'^\d+(\.\d{1,6})?$')
    minimum_quality: float = Field(ge=0, le=1)
    task_type: str = 'general'


class ExecuteRequest(BaseModel):
    mission_id: str
    model_id: str
    execution_id: str = Field(min_length=1)
    reservation_id: str | None = None
    cpu_seconds: float = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)
    io_read_bytes: int = Field(ge=0)
    io_write_bytes: int = Field(ge=0)
    token_count: int = Field(default=0, ge=0)
    wall_seconds: float = Field(default=0, ge=0)
    outcome: str = Field(pattern=r'^(verified_success|failed)$')
    evidence: dict[str, Any] = Field(default_factory=dict)


class MicroFundRequest(BaseModel):
    amount_micro_cents: int = Field(ge=0)


class MicroEscrowRequest(BaseModel):
    task_id: str = Field(min_length=1)
    payer_agent_id: str = Field(min_length=1)
    lock_amount_micro_cents: int = Field(ge=0)


class TelemetryRequest(BaseModel):
    event_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    payer_agent_id: str = Field(min_length=1)
    provider_agent_id: str = Field(min_length=1)
    metric_type: MetricType
    quantity: int = Field(ge=1)
    unit_price_micro_cents: int = Field(ge=0)
    total_charge_micro_cents: int = Field(ge=0)
    timestamp: str
    signature: str | None = None


class SplitRuleRequest(BaseModel):
    beneficiary_id: str = Field(min_length=1)
    basis_points: int = Field(ge=0, le=10000)
    role: str = Field(min_length=1)


class MicroSettlementRequest(BaseModel):
    contract_id: str = Field(min_length=1)
    rules: list[SplitRuleRequest] = Field(min_length=1)


class MemoryRequest(BaseModel):
    content: str = Field(min_length=1)
    memory_type: str | None = None
    source_id: str | None = None
    actor: str | None = None


class MemorySearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1)


class MemoryRetractRequest(BaseModel):
    reason: str = ''
    actor: str = 'system'


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def ledger(db: sqlite3.Connection, wallet_id: str, event: str, amount: Decimal, *, mission_id: str | None = None, reservation_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    db.execute('INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (str(uuid.uuid4()), wallet_id, mission_id, reservation_id, event, money_str(amount), json.dumps(metadata or {}, sort_keys=True), now()))


def require_wallet(db: sqlite3.Connection, wallet_id: str) -> sqlite3.Row:
    wallet = db.execute('SELECT * FROM wallets WHERE id = ?', (wallet_id,)).fetchone()
    if not wallet:
        raise HTTPException(404, 'wallet not found')
    return wallet


@app.get('/health')
def health() -> dict[str, Any]:
    memory_health = MEMORY.health()
    return {
        'status': 'ok' if memory_health['ledger_verified'] else 'degraded',
        'service': 'sovereign-economic-engine',
        'components': {
            'economic_wallet': 'ready',
            'micro_billing': 'ready_in_process_memory',
            'memory': memory_health,
            'external_model_executor': 'not_connected',
        },
    }


@app.post('/wallets', status_code=201)
def create_wallet(payload: WalletCreate) -> dict[str, Any]:
    try:
        capital = money(payload.capital)
        minimum = money(payload.minimum_liquidity)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if minimum > capital:
        raise HTTPException(422, 'minimum_liquidity cannot exceed capital')
    wallet_id = f'wallet_{uuid.uuid4().hex[:12]}'
    with closing(connect()) as db:
        db.execute('INSERT INTO wallets VALUES (?, ?, ?, ?, ?, ?, ?)', (wallet_id, money_str(capital), money_str(capital), '0.000000', '0.000000', money_str(minimum), now()))
        ledger(db, wallet_id, 'deposit', capital, metadata={'source': 'initial_capital'})
        db.commit()
        return row_dict(db.execute('SELECT * FROM wallets WHERE id = ?', (wallet_id,)).fetchone()) or {}


@app.get('/wallets/{wallet_id}')
def get_wallet(wallet_id: str) -> dict[str, Any]:
    with closing(connect()) as db:
        return row_dict(require_wallet(db, wallet_id)) or {}


@app.post('/models', status_code=201)
def register_model(payload: ModelCreate) -> dict[str, Any]:
    model_id = payload.id or f'model_{uuid.uuid4().hex[:12]}'
    with closing(connect()) as db:
        try:
            cost = money(payload.cost_per_task)
            db.execute('INSERT INTO models VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (model_id, payload.name, json.dumps(payload.task_types), payload.quality, payload.reliability, money_str(cost), payload.latency_ms, 'qualified', now()))
            db.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, 'model id already exists') from exc
        return row_dict(db.execute('SELECT * FROM models WHERE id = ?', (model_id,)).fetchone()) or {}


@app.get('/models')
def list_models() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM models WHERE status = 'qualified' ORDER BY cost_per_task ASC").fetchall()
        return [row_dict(row) or {} for row in rows]


@app.post('/missions', status_code=201)
def create_mission(payload: MissionCreate) -> dict[str, Any]:
    try:
        max_cost = money(payload.max_cost)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    mission_id = f'mission_{uuid.uuid4().hex[:12]}'
    with closing(connect()) as db:
        require_wallet(db, payload.wallet_id)
        db.execute('INSERT INTO missions (id, wallet_id, description, max_cost, minimum_quality, task_type, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (mission_id, payload.wallet_id, payload.description, money_str(max_cost), payload.minimum_quality, payload.task_type, 'planned', now()))
        db.commit()
        return row_dict(db.execute('SELECT * FROM missions WHERE id = ?', (mission_id,)).fetchone()) or {}


@app.post('/missions/{mission_id}/decide')
def decide_route(mission_id: str) -> dict[str, Any]:
    with closing(connect()) as db:
        mission = db.execute('SELECT * FROM missions WHERE id = ?', (mission_id,)).fetchone()
        if not mission:
            raise HTTPException(404, 'mission not found')
        models = db.execute("SELECT * FROM models WHERE status = 'qualified'").fetchall()
        candidates = []
        for model in models:
            task_types = json.loads(model['task_types'])
            if mission['task_type'] in task_types and mission['minimum_quality'] <= model['quality'] * model['reliability'] and money(model['cost_per_task']) <= money(mission['max_cost']):
                fit = model['quality'] * model['reliability'] / max(float(money(model['cost_per_task'])), 0.000001)
                candidates.append((fit, model))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            decision = {'status': 'HOLD', 'reason': 'NO_QUALIFIED_MODEL_WITHIN_ECONOMIC_ENVELOPE'}
            selected = None
        else:
            selected = candidates[0][1]
            decision = {'status': 'APPROVED', 'reason': 'MODEL_MEETS_QUALITY_AND_COST_CONSTRAINTS', 'selected_model': selected['id']}
        decision_id = f'decision_{uuid.uuid4().hex[:12]}'
        db.execute('INSERT INTO routing_decisions VALUES (?, ?, ?, ?, ?, ?)', (decision_id, mission_id, selected['id'] if selected else None, json.dumps(decision, sort_keys=True), json.dumps([{'model_id': item[1]['id'], 'cost': item[1]['cost_per_task'], 'quality': item[1]['quality']} for item in candidates[1:]], sort_keys=True), now()))
        db.commit()
        return {'decision_id': decision_id, **decision, 'alternatives': json.loads(db.execute('SELECT alternatives FROM routing_decisions WHERE id = ?', (decision_id,)).fetchone()['alternatives'])}


@app.post('/executions', status_code=201)
def execute(payload: ExecuteRequest) -> dict[str, Any]:
    with closing(connect()) as db:
        existing = db.execute('SELECT * FROM settlements WHERE execution_id = ?', (payload.execution_id,)).fetchone()
        if existing:
            wallet = require_wallet(db, existing['wallet_id'])
            return {'execution_id': payload.execution_id, 'settlement_id': existing['settlement_id'], 'amount': existing['amount'], 'idempotent_replay': True, 'wallet': row_dict(wallet) or {}}
        mission = db.execute('SELECT * FROM missions WHERE id = ?', (payload.mission_id,)).fetchone()
        model = db.execute('SELECT * FROM models WHERE id = ?', (payload.model_id,)).fetchone()
        if not mission or not model:
            raise HTTPException(404, 'mission or model not found')
        if mission['status'] not in ('planned', 'approved'):
            raise HTTPException(409, f'mission is not executable from status {mission["status"]}')
        usage = ResourceUsage(Decimal(str(payload.cpu_seconds)), payload.peak_memory_bytes, payload.io_read_bytes, payload.io_write_bytes, payload.token_count, Decimal(str(payload.wall_seconds)))
        actual = money(PricingPolicy().price(usage))
        wallet = require_wallet(db, mission['wallet_id'])
        available = money(wallet['available'])
        minimum = money(wallet['minimum_liquidity'])
        if actual > money(mission['max_cost']):
            raise HTTPException(403, 'measured cost exceeds mission economic envelope')
        reservation_id = payload.reservation_id or f'reservation_{uuid.uuid4().hex[:12]}'
        reservation_amount = money(mission['max_cost'])
        if available - reservation_amount < minimum:
            raise HTTPException(403, 'reservation would violate minimum liquidity protection')
        db.execute('INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?)', (reservation_id, wallet['id'], mission['id'], money_str(reservation_amount), 'reserved', now(), None))
        db.execute('UPDATE wallets SET available = ?, reserved = ? WHERE id = ?', (money_str(available - reservation_amount), money_str(money(wallet['reserved']) + reservation_amount), wallet['id']))
        ledger(db, wallet['id'], 'reserve', reservation_amount, mission_id=mission['id'], reservation_id=reservation_id, metadata={'model_id': model['id'], 'execution_id': payload.execution_id})
        settlement_id = f'settlement_{hashlib.sha256(payload.execution_id.encode()).hexdigest()[:24]}'
        previous = db.execute('SELECT event_hash FROM settlements ORDER BY created_at DESC LIMIT 1').fetchone()
        previous_hash = previous['event_hash'] if previous else ''
        event_payload = {'execution_id': payload.execution_id, 'reservation_id': reservation_id, 'amount': money_str(actual), 'previous_event_hash': previous_hash, 'usage': usage.as_dict(), 'outcome': payload.outcome}
        event_hash = hashlib.sha256(json.dumps(event_payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        db.execute('INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (payload.execution_id, mission['id'], model['id'], reservation_id, json.dumps(usage.as_dict(), sort_keys=True), payload.outcome, json.dumps(payload.evidence, sort_keys=True), 'settlement_pending', now()))
        unused = reservation_amount - actual
        event = 'outcome' if payload.outcome == 'verified_success' else 'failure'
        db.execute('UPDATE reservations SET status = ?, settled_at = ? WHERE id = ?', ('settled', now(), reservation_id))
        db.execute('UPDATE wallets SET available = ?, reserved = ?, spent = ? WHERE id = ?', (money_str(available - reservation_amount + unused), money_str(money(wallet['reserved']) + reservation_amount - actual), money_str(money(wallet['spent']) + actual), wallet['id']))
        db.execute('UPDATE missions SET status = ? WHERE id = ?', ('completed' if payload.outcome == 'verified_success' else 'failed', mission['id']))
        ledger(db, wallet['id'], event, actual, mission_id=mission['id'], reservation_id=reservation_id, metadata={'model_id': model['id'], 'execution_id': payload.execution_id, 'usage': usage.as_dict(), 'evidence': payload.evidence, 'outcome': payload.outcome})
        if unused:
            ledger(db, wallet['id'], 'release', unused, mission_id=mission['id'], reservation_id=reservation_id, metadata={'execution_id': payload.execution_id})
        db.execute('UPDATE executions SET status = ? WHERE execution_id = ?', ('settled', payload.execution_id))
        db.execute('INSERT INTO settlements VALUES (?, ?, ?, ?, ?, ?, ?)', (settlement_id, payload.execution_id, wallet['id'], money_str(actual), previous_hash, event_hash, now()))
        db.commit()
        result = row_dict(db.execute('SELECT * FROM wallets WHERE id = ?', (wallet['id'],)).fetchone()) or {}
        return {'execution_id': payload.execution_id, 'settlement_id': settlement_id, 'mission_id': mission['id'], 'reservation_id': reservation_id, 'outcome': payload.outcome, 'usage': usage.as_dict(), 'amount': money_str(actual), 'wallet': result}


@app.get('/ledger/{wallet_id}')
def get_ledger(wallet_id: str) -> list[dict[str, Any]]:
    with closing(connect()) as db:
        require_wallet(db, wallet_id)
        return [row_dict(row) or {} for row in db.execute('SELECT * FROM ledger WHERE wallet_id = ? ORDER BY created_at ASC', (wallet_id,)).fetchall()]


@app.post('/micro/wallets/{agent_id}/fund')
def micro_fund(agent_id: str, payload: MicroFundRequest) -> dict[str, Any]:
    try:
        balance = MICRO_BILLING.fund_wallet(agent_id, payload.amount_micro_cents)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {'agent_id': agent_id, 'balance_micro_cents': balance}


@app.get('/micro/wallets/{agent_id}')
def micro_balance(agent_id: str) -> dict[str, Any]:
    return {'agent_id': agent_id, 'balance_micro_cents': MICRO_BILLING.wallets.get(agent_id, 0)}


@app.post('/micro/escrows', status_code=201)
def micro_create_escrow(payload: MicroEscrowRequest) -> dict[str, Any]:
    try:
        hold = MICRO_BILLING.create_escrow_hold(payload.task_id, payload.payer_agent_id, payload.lock_amount_micro_cents)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'hold_id': hold.hold_id, 'task_id': hold.task_id, 'payer_agent_id': hold.payer_agent_id, 'locked_amount_micro_cents': hold.locked_amount_micro_cents, 'consumed_amount_micro_cents': hold.consumed_amount_micro_cents, 'status': hold.status.value, 'expires_at': hold.expires_at.isoformat()}


@app.get('/micro/escrows/{task_id}')
def micro_get_escrow(task_id: str) -> dict[str, Any]:
    hold = MICRO_BILLING.escrows.get(task_id)
    if not hold:
        raise HTTPException(404, 'micro escrow not found')
    return {'hold_id': hold.hold_id, 'task_id': hold.task_id, 'payer_agent_id': hold.payer_agent_id, 'locked_amount_micro_cents': hold.locked_amount_micro_cents, 'consumed_amount_micro_cents': hold.consumed_amount_micro_cents, 'refunded_amount_micro_cents': hold.refunded_amount_micro_cents, 'status': hold.status.value, 'expires_at': hold.expires_at.isoformat()}


@app.post('/micro/telemetry')
def micro_record_telemetry(payload: TelemetryRequest) -> dict[str, Any]:
    try:
        hold = MICRO_BILLING.record_telemetry(TelemetryEvent(**payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'task_id': hold.task_id, 'consumed_amount_micro_cents': hold.consumed_amount_micro_cents, 'remaining_amount_micro_cents': hold.locked_amount_micro_cents - hold.consumed_amount_micro_cents, 'status': hold.status.value}


@app.post('/micro/escrows/{task_id}/settle')
def micro_settle(task_id: str, payload: MicroSettlementRequest) -> dict[str, Any]:
    try:
        contract = SplitRevenueContract(payload.contract_id, tuple(SplitRule(**rule.model_dump()) for rule in payload.rules))
        return MICRO_BILLING.settle_task(task_id, contract)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/micro/escrows/{task_id}/expire')
def micro_expire(task_id: str) -> dict[str, Any]:
    try:
        hold = MICRO_BILLING.expire_task(task_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'task_id': hold.task_id, 'status': hold.status.value, 'refunded_amount_micro_cents': hold.refunded_amount_micro_cents}


@app.post('/memory/remember', status_code=201)
def memory_remember(payload: MemoryRequest) -> dict[str, Any]:
    try:
        return MEMORY.remember(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post('/memory/search')
def memory_search(payload: MemorySearchRequest) -> list[dict[str, Any]]:
    try:
        return MEMORY.search(payload.query, payload.top_k)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get('/memory/health')
def memory_health() -> dict[str, Any]:
    return MEMORY.health()


@app.get('/memory/{memory_id}')
def memory_get(memory_id: str) -> dict[str, Any]:
    result = MEMORY.get(memory_id)
    if result is None:
        raise HTTPException(404, 'memory not found')
    return result


@app.get('/memory/{memory_id}/explain')
def memory_explain(memory_id: str) -> dict[str, Any]:
    result = MEMORY.explain(memory_id)
    if result['memory'] is None:
        raise HTTPException(404, 'memory not found')
    return result


@app.post('/memory/{memory_id}/retract')
def memory_retract(memory_id: str, payload: MemoryRetractRequest) -> dict[str, Any]:
    try:
        return MEMORY.retract(memory_id, payload.reason, payload.actor)
    except KeyError as exc:
        raise HTTPException(404, 'memory not found') from exc


init_db()
