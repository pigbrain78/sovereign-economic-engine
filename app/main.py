from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from app.substrate import PricingPolicy, ResourceUsage
from app.memory import MemoryAPI
from app.micro_billing import MetricType, SplitRevenueContract, SplitRule, SynapseMicroBillingEngine, TelemetryEvent
from app.huggingface_layer import HuggingFaceProviderUnavailable, ModelPolicyViolation, HuggingFaceChatAdapter, catalog as hf_catalog, quote_model as hf_quote_model, select_model as hf_select_model
from app.upgrade_treasury import RDPFitnessMetric, ReserveRates, ScenarioPath, ScenarioSimulator, UpgradeStatus, canonical_approval_payload, verify_ed25519_signature
from app.sandbox_promotion import SandboxPromotion, SandboxRejected
from app.local_executor import LocalCommandExecutor, LocalExecutorUnavailable
from app.local_model import LocalModelUnavailable, local_model_status, select_local_model

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(__import__('os').environ.get('SOVEREIGN_DB', BASE_DIR / 'sovereign.db'))

app = FastAPI(title='Sovereign Economic Engine', version='0.1.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)
MICRO_BILLING = SynapseMicroBillingEngine()
MEMORY = MemoryAPI(__import__('os').environ.get('SOVEREIGN_MEMORY_DB', BASE_DIR / 'sovereign-memory.db'))
SANDBOX = SandboxPromotion(__import__('os').environ.get('SOVEREIGN_SANDBOX_ROOT', BASE_DIR / '.sandbox'))


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
            development_reserve TEXT NOT NULL DEFAULT '0.000000',
            safety_reserve TEXT NOT NULL DEFAULT '0.000000',
            owner_reserve TEXT NOT NULL DEFAULT '0.000000',
            development_rate_bps INTEGER NOT NULL DEFAULT 1000,
            safety_rate_bps INTEGER NOT NULL DEFAULT 500,
            owner_rate_bps INTEGER NOT NULL DEFAULT 500,
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
        CREATE TABLE IF NOT EXISTS execution_envelopes (
            envelope_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL UNIQUE REFERENCES missions(id),
            memory_id TEXT NOT NULL,
            wallet_id TEXT NOT NULL REFERENCES wallets(id),
            privacy_mode TEXT NOT NULL,
            runtime_policy TEXT NOT NULL,
            task_type TEXT NOT NULL,
            max_cost TEXT NOT NULL,
            minimum_quality REAL NOT NULL,
            capabilities TEXT NOT NULL,
            quote_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_executions (
            execution_id TEXT PRIMARY KEY,
            envelope_id TEXT NOT NULL REFERENCES execution_envelopes(envelope_id),
            mission_id TEXT NOT NULL REFERENCES missions(id),
            reservation_id TEXT,
            status TEXT NOT NULL,
            output TEXT NOT NULL,
            stderr TEXT NOT NULL,
            usage TEXT NOT NULL,
            amount TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pocket_projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pocket_loops (
            loop_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES pocket_projects(project_id),
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pocket_shadow (
            shadow_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES pocket_projects(project_id),
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence TEXT NOT NULL,
            authority TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pocket_proposals (
            proposal_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES pocket_projects(project_id),
            loop_id TEXT REFERENCES pocket_loops(loop_id),
            shadow_id TEXT REFERENCES pocket_shadow(shadow_id),
            agent_id TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            candidate TEXT NOT NULL,
            proposed_cost TEXT NOT NULL,
            recurring INTEGER NOT NULL,
            access_level TEXT NOT NULL,
            expected_benefit TEXT NOT NULL,
            evidence TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            status TEXT NOT NULL,
            authority TEXT NOT NULL,
            execution_authorized INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            approved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pocket_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS upgrade_proposals (
            proposal_id TEXT PRIMARY KEY,
            wallet_id TEXT NOT NULL REFERENCES wallets(id),
            target_module TEXT NOT NULL,
            estimated_cost TEXT NOT NULL,
            predicted_monthly_value TEXT NOT NULL,
            predicted_roi REAL NOT NULL,
            actual_roi REAL,
            status TEXT NOT NULL,
            rdp_metrics TEXT,
            scenario_paths TEXT NOT NULL,
            selected_path TEXT,
            signature_proof TEXT,
            approval_actor TEXT,
            reserved_amount TEXT NOT NULL DEFAULT '0.000000',
            evidence TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS upgrade_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            proposal_id TEXT NOT NULL REFERENCES upgrade_proposals(proposal_id),
            event_type TEXT NOT NULL,
            amount TEXT NOT NULL,
            payload TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promotion_records (
            promotion_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL UNIQUE REFERENCES upgrade_proposals(proposal_id),
            candidate_id TEXT NOT NULL,
            candidate_hash TEXT NOT NULL,
            verification_receipt TEXT NOT NULL,
            canary_percent INTEGER NOT NULL,
            status TEXT NOT NULL,
            promotion_receipt TEXT,
            rollback_receipt TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        ''')
        mission_columns = {row['name'] for row in db.execute('PRAGMA table_info(missions)').fetchall()}
        if 'task_type' not in mission_columns:
            db.execute("ALTER TABLE missions ADD COLUMN task_type TEXT NOT NULL DEFAULT 'general'")
        wallet_columns = {row['name'] for row in db.execute('PRAGMA table_info(wallets)').fetchall()}
        for column, definition in {
            'development_reserve': "TEXT NOT NULL DEFAULT '0.000000'",
            'safety_reserve': "TEXT NOT NULL DEFAULT '0.000000'",
            'owner_reserve': "TEXT NOT NULL DEFAULT '0.000000'",
            'development_rate_bps': 'INTEGER NOT NULL DEFAULT 1000',
            'safety_rate_bps': 'INTEGER NOT NULL DEFAULT 500',
            'owner_rate_bps': 'INTEGER NOT NULL DEFAULT 500',
        }.items():
            if column not in wallet_columns:
                db.execute(f'ALTER TABLE wallets ADD COLUMN {column} {definition}')
        db.commit()


@app.on_event('startup')
def startup() -> None:
    init_db()


class WalletCreate(BaseModel):
    capital: str = Field(pattern=r'^\d+(\.\d{1,6})?$')
    minimum_liquidity: str = Field(default='0.000000', pattern=r'^\d+(\.\d{1,6})?$')
    development_rate_bps: int = Field(default=1000, ge=0, le=10000)
    safety_rate_bps: int = Field(default=500, ge=0, le=10000)
    owner_rate_bps: int = Field(default=500, ge=0, le=10000)


class WalletDeposit(BaseModel):
    amount: str = Field(pattern=r'^\d+(\.\d{1,6})?$')


class UpgradeProposalCreate(BaseModel):
    wallet_id: str
    target_module: str = Field(min_length=1)
    estimated_cost: str = Field(pattern=r'^\d+(\.\d{1,6})?$')
    predicted_monthly_value: str = Field(default='0.000000', pattern=r'^\d+(\.\d{1,6})?$')
    scenario_paths: list[dict[str, Any]] = Field(default_factory=list)


class UpgradeEvaluation(BaseModel):
    mutation_id: str = Field(min_length=1)
    ast_node_coverage: float = Field(ge=0, le=1)
    failure_reduction_rate: float = Field(ge=0, le=1)
    fitness_score: float = Field(ge=0, le=1)
    verifier_passed: bool
    evidence: dict[str, Any] = Field(default_factory=dict)


class UpgradeApproval(BaseModel):
    actor: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)


class UpgradeCompletion(BaseModel):
    verified: bool
    actual_monthly_value: str = Field(default='0.000000', pattern=r'^\d+(\.\d{1,6})?$')
    evidence: dict[str, Any] = Field(default_factory=dict)


class PromotionCreate(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=96)
    files: dict[str, str] = Field(min_length=1)
    canary_percent: int = Field(default=10, ge=1, le=100)


class CanaryActivation(BaseModel):
    canary_passed: bool
    evidence: dict[str, Any] = Field(default_factory=dict)


class RollbackRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


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


class ThoughtCaptureRequest(BaseModel):
    wallet_id: str
    content: str = Field(min_length=1, max_length=20000)
    task_type: str = Field(default='project_improvement', min_length=1, max_length=80)
    max_cost: str = Field(default='0.050000', pattern=r'^\d+(\.\d{1,6})?$')
    minimum_quality: float = Field(default=0.5, ge=0, le=1)
    privacy_mode: str = Field(default='local_only', pattern=r'^(local_only|user_server|approved_external)$')
    capabilities: list[str] = Field(default_factory=lambda: ['project_analysis'])
    project_id: str | None = None
    actor: str = Field(default='operator', min_length=1, max_length=120)


class LocalExecuteRequest(BaseModel):
    execution_id: str = Field(min_length=1, max_length=160)
    model_id: str | None = Field(default=None, max_length=160)
    timeout_seconds: float = Field(default=30, gt=0, le=120)


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


class PocketProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class PocketLoopCreate(BaseModel):
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)


class ShadowCreate(BaseModel):
    kind: str = Field(pattern=r'^(observation|insight|warning|opportunity|question|recommendation)$')
    content: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ProposalCreate(BaseModel):
    project_id: str
    loop_id: str | None = None
    shadow_id: str | None = None
    agent_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    candidate: dict[str, Any]
    proposed_cost: str = Field(pattern=r'^\d+(\.\d{1,6})?$')
    recurring: bool = False
    access_level: str = Field(pattern=r'^(none|read_only|write|admin)$')
    expected_benefit: dict[str, Any]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    recommendation: str = Field(pattern=r'^(REJECT|TRIAL_EXTENDED|HOLD_FOR_APPROVAL|APPROVED_TO_PURCHASE)$')


class ProposalApproval(BaseModel):
    decision: str = Field(pattern=r'^(approve|reject|request_more_testing)$')
    actor: str = Field(min_length=1)


class HuggingFaceQuoteRequest(BaseModel):
    capabilities: list[str] = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    max_cost: str = Field(pattern=r'^\d+(\.\d{1,6})?$')
    minimum_quality: float = Field(default=0, ge=0, le=1)
    routing_policy: str = Field(default='cheapest', pattern=r'^(cheapest|fastest|preferred)$')
    preferred_providers: list[str] = Field(default_factory=list)
    model_id: str | None = None


class HuggingFaceChatRequest(HuggingFaceQuoteRequest):
    messages: list[dict[str, str]] = Field(min_length=1)
    max_tokens: int = Field(default=512, gt=0, le=8192)
    provider: str | None = None


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def ledger(db: sqlite3.Connection, wallet_id: str, event: str, amount: Decimal, *, mission_id: str | None = None, reservation_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    db.execute('INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (str(uuid.uuid4()), wallet_id, mission_id, reservation_id, event, money_str(amount), json.dumps(metadata or {}, sort_keys=True), now()))


def pocket_event(db: sqlite3.Connection, event_type: str, entity_id: str, payload: dict[str, Any]) -> None:
    previous = db.execute('SELECT event_hash FROM pocket_events ORDER BY sequence DESC LIMIT 1').fetchone()
    previous_hash = previous['event_hash'] if previous else '0' * 64
    created_at = now()
    body = {'event_type': event_type, 'entity_id': entity_id, 'payload': payload, 'previous_hash': previous_hash, 'created_at': created_at}
    event_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    event_id = hashlib.sha256(event_hash.encode()).hexdigest()[:32]
    db.execute('INSERT INTO pocket_events(event_id,event_type,entity_id,payload,previous_hash,event_hash,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)', (event_id, event_type, entity_id, json.dumps(payload, sort_keys=True), previous_hash, event_hash, created_at))


def verify_pocket_events(db: sqlite3.Connection) -> bool:
    previous_hash = '0' * 64
    for row in db.execute('SELECT * FROM pocket_events ORDER BY sequence').fetchall():
        if row['previous_hash'] != previous_hash:
            return False
        body = {'event_type': row['event_type'], 'entity_id': row['entity_id'], 'payload': json.loads(row['payload']), 'previous_hash': row['previous_hash'], 'created_at': row['created_at']}
        expected = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if expected != row['event_hash']:
            return False
        previous_hash = row['event_hash']
    return True


def upgrade_event(db: sqlite3.Connection, proposal_id: str, event_type: str, amount: Decimal, payload: dict[str, Any]) -> None:
    previous = db.execute('SELECT event_hash FROM upgrade_events ORDER BY sequence DESC LIMIT 1').fetchone()
    previous_hash = previous['event_hash'] if previous else '0' * 64
    created_at = now()
    body = {'proposal_id': proposal_id, 'event_type': event_type, 'amount': money_str(amount), 'payload': payload, 'previous_hash': previous_hash, 'created_at': created_at}
    event_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    db.execute('INSERT INTO upgrade_events(event_id,proposal_id,event_type,amount,payload,previous_hash,event_hash,created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (hashlib.sha256(event_hash.encode()).hexdigest()[:32], proposal_id, event_type, money_str(amount), json.dumps(payload, sort_keys=True), previous_hash, event_hash, created_at))


def verify_upgrade_events(db: sqlite3.Connection) -> bool:
    previous_hash = '0' * 64
    for row in db.execute('SELECT * FROM upgrade_events ORDER BY sequence').fetchall():
        if row['previous_hash'] != previous_hash:
            return False
        body = {'proposal_id': row['proposal_id'], 'event_type': row['event_type'], 'amount': row['amount'], 'payload': json.loads(row['payload']), 'previous_hash': row['previous_hash'], 'created_at': row['created_at']}
        expected = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if expected != row['event_hash']:
            return False
        previous_hash = row['event_hash']
    return True


def reserve_totals(wallet: sqlite3.Row) -> Decimal:
    return money(wallet['available']) + money(wallet['reserved']) + money(wallet['spent']) + money(wallet['development_reserve']) + money(wallet['safety_reserve']) + money(wallet['owner_reserve'])


def require_project(db: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    project = db.execute('SELECT * FROM pocket_projects WHERE project_id = ?', (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, 'Pocket OS project not found')
    return project


def require_proposal(db: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    proposal = db.execute('SELECT * FROM pocket_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()
    if not proposal:
        raise HTTPException(404, 'Pocket OS proposal not found')
    return proposal


def require_wallet(db: sqlite3.Connection, wallet_id: str) -> sqlite3.Row:
    wallet = db.execute('SELECT * FROM wallets WHERE id = ?', (wallet_id,)).fetchone()
    if not wallet:
        raise HTTPException(404, 'wallet not found')
    return wallet


def require_upgrade(db: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    proposal = db.execute('SELECT * FROM upgrade_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()
    if not proposal:
        raise HTTPException(404, 'upgrade proposal not found')
    return proposal


@app.get('/health')
def health() -> dict[str, Any]:
    memory_health = MEMORY.health()
    with closing(connect()) as db:
        pocket_ledger_verified = verify_pocket_events(db)
        upgrade_ledger_verified = verify_upgrade_events(db)
    hf_models = hf_catalog()
    hf_ready = bool(os.environ.get('HF_TOKEN')) and any(model.pricing_verified for model in hf_models)
    return {
        'status': 'ok' if memory_health['ledger_verified'] and pocket_ledger_verified and upgrade_ledger_verified else 'degraded',
        'service': 'sovereign-economic-engine',
        'components': {
            'economic_wallet': 'ready',
            'micro_billing': 'ready_in_process_memory',
            'memory': memory_health,
            'pocket_os': {'status': 'ok', 'event_ledger_verified': pocket_ledger_verified},
            'upgrade_treasury': {'status': 'ok', 'event_ledger_verified': upgrade_ledger_verified},
            'external_model_executor': 'huggingface_ready' if hf_ready else 'huggingface_config_required',
        },
    }


@app.post('/pocket/projects', status_code=201)
def create_pocket_project(payload: PocketProjectCreate) -> dict[str, Any]:
    project_id = f'project_{uuid.uuid4().hex[:12]}'
    with closing(connect()) as db:
        db.execute('INSERT INTO pocket_projects VALUES (?, ?, ?, ?, ?)', (project_id, payload.name, payload.description, 'active', now()))
        pocket_event(db, 'PROJECT_CREATED', project_id, payload.model_dump())
        db.commit()
        return row_dict(db.execute('SELECT * FROM pocket_projects WHERE project_id = ?', (project_id,)).fetchone()) or {}


@app.get('/pocket/projects')
def list_pocket_projects() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [row_dict(row) or {} for row in db.execute('SELECT * FROM pocket_projects ORDER BY created_at DESC').fetchall()]


@app.post('/pocket/projects/{project_id}/loops', status_code=201)
def create_pocket_loop(project_id: str, payload: PocketLoopCreate) -> dict[str, Any]:
    loop_id = f'loop_{uuid.uuid4().hex[:12]}'
    with closing(connect()) as db:
        require_project(db, project_id)
        db.execute('INSERT INTO pocket_loops VALUES (?, ?, ?, ?, ?, ?)', (loop_id, project_id, payload.title, payload.objective, 'open', now()))
        pocket_event(db, 'OPEN_LOOP_CREATED', loop_id, {'project_id': project_id, **payload.model_dump()})
        db.commit()
        return row_dict(db.execute('SELECT * FROM pocket_loops WHERE loop_id = ?', (loop_id,)).fetchone()) or {}


@app.post('/pocket/projects/{project_id}/shadow', status_code=201)
def create_shadow_observation(project_id: str, payload: ShadowCreate) -> dict[str, Any]:
    shadow_id = f'shadow_{uuid.uuid4().hex[:12]}'
    with closing(connect()) as db:
        require_project(db, project_id)
        db.execute('INSERT INTO pocket_shadow VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (shadow_id, project_id, payload.kind, payload.content, payload.confidence, json.dumps(payload.evidence, sort_keys=True), 'NONE', now()))
        pocket_event(db, 'SHADOW_OBSERVED', shadow_id, {'project_id': project_id, **payload.model_dump(), 'authority': 'NONE', 'can_execute': False, 'can_ratify': False})
        db.commit()
        return {**(row_dict(db.execute('SELECT * FROM pocket_shadow WHERE shadow_id = ?', (shadow_id,)).fetchone()) or {}), 'authority': 'NONE', 'can_execute': False, 'can_ratify': False}


@app.get('/pocket/shadow')
def list_shadow_observations(project_id: str | None = None) -> list[dict[str, Any]]:
    with closing(connect()) as db:
        if project_id:
            require_project(db, project_id)
            rows = db.execute('SELECT * FROM pocket_shadow WHERE project_id = ? ORDER BY created_at DESC', (project_id,)).fetchall()
        else:
            rows = db.execute('SELECT * FROM pocket_shadow ORDER BY created_at DESC').fetchall()
        return [{**(row_dict(row) or {}), 'authority': 'NONE', 'can_execute': False, 'can_ratify': False} for row in rows]


@app.post('/pocket/proposals', status_code=201)
def create_pocket_proposal(payload: ProposalCreate) -> dict[str, Any]:
    try:
        cost = money(payload.proposed_cost)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    proposal_id = f'proposal_{uuid.uuid4().hex[:12]}'
    initial_status = 'REJECTED' if payload.recommendation == 'REJECT' else ('HOLD_FOR_APPROVAL' if payload.recommendation == 'APPROVED_TO_PURCHASE' else payload.recommendation)
    with closing(connect()) as db:
        require_project(db, payload.project_id)
        if payload.loop_id:
            loop = db.execute('SELECT * FROM pocket_loops WHERE loop_id = ? AND project_id = ?', (payload.loop_id, payload.project_id)).fetchone()
            if not loop:
                raise HTTPException(422, 'loop does not belong to project')
        if payload.shadow_id:
            shadow = db.execute('SELECT * FROM pocket_shadow WHERE shadow_id = ? AND project_id = ?', (payload.shadow_id, payload.project_id)).fetchone()
            if not shadow:
                raise HTTPException(422, 'shadow observation does not belong to project')
        if not payload.evidence and initial_status in {'HOLD_FOR_APPROVAL', 'APPROVED_TO_PURCHASE'}:
            raise HTTPException(422, 'purchase recommendations require evidence')
        db.execute('INSERT INTO pocket_proposals (proposal_id, project_id, loop_id, shadow_id, agent_id, title, category, candidate, proposed_cost, recurring, access_level, expected_benefit, evidence, recommendation, status, authority, execution_authorized, created_at, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (proposal_id, payload.project_id, payload.loop_id, payload.shadow_id, payload.agent_id, payload.title, payload.category, json.dumps(payload.candidate, sort_keys=True), money_str(cost), int(payload.recurring), payload.access_level, json.dumps(payload.expected_benefit, sort_keys=True), json.dumps(payload.evidence, sort_keys=True), payload.recommendation, initial_status, 'NONE', 0, now(), None))
        pocket_event(db, 'PROPOSAL_CREATED', proposal_id, {'project_id': payload.project_id, 'agent_id': payload.agent_id, 'recommendation': payload.recommendation, 'status': initial_status, 'authority': 'NONE'})
        db.commit()
        result = row_dict(db.execute('SELECT * FROM pocket_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()) or {}
        result['execution_authorized'] = False
        return result


@app.get('/pocket/proposals')
def list_pocket_proposals(status: str | None = None) -> list[dict[str, Any]]:
    with closing(connect()) as db:
        if status:
            rows = db.execute('SELECT * FROM pocket_proposals WHERE status = ? ORDER BY created_at DESC', (status,)).fetchall()
        else:
            rows = db.execute('SELECT * FROM pocket_proposals ORDER BY created_at DESC').fetchall()
        return [{**(row_dict(row) or {}), 'execution_authorized': False} for row in rows]


@app.post('/pocket/proposals/{proposal_id}/decision')
def decide_pocket_proposal(proposal_id: str, payload: ProposalApproval) -> dict[str, Any]:
    with closing(connect()) as db:
        proposal = require_proposal(db, proposal_id)
        if payload.decision == 'approve':
            if proposal['status'] != 'HOLD_FOR_APPROVAL':
                raise HTTPException(409, 'only a proposal in HOLD_FOR_APPROVAL can be approved')
            status = 'USER_APPROVED_PENDING_EXTERNAL_PURCHASE'
            authority = 'HUMAN_RATIFIED'
            approved_at = now()
        elif payload.decision == 'reject':
            status, authority, approved_at = 'REJECTED', 'HUMAN_RATIFIED', None
        else:
            status, authority, approved_at = 'TRIAL_EXTENDED', 'HUMAN_RATIFIED', None
        db.execute('UPDATE pocket_proposals SET status = ?, authority = ?, approved_at = ? WHERE proposal_id = ?', (status, authority, approved_at, proposal_id))
        pocket_event(db, 'PROPOSAL_DECIDED', proposal_id, {'decision': payload.decision, 'actor': payload.actor, 'status': status, 'execution_authorized': False})
        db.commit()
        result = row_dict(db.execute('SELECT * FROM pocket_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()) or {}
        result['execution_authorized'] = False
        result['note'] = 'Approval records a human decision only; it does not purchase, reserve, or authorize execution.'
        return result


@app.get('/pocket/events')
def list_pocket_events() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [row_dict(row) or {} for row in db.execute('SELECT * FROM pocket_events ORDER BY sequence ASC').fetchall()]


@app.post('/wallets', status_code=201)
def create_wallet(payload: WalletCreate) -> dict[str, Any]:
    try:
        capital = money(payload.capital)
        minimum = money(payload.minimum_liquidity)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if minimum > capital:
        raise HTTPException(422, 'minimum_liquidity cannot exceed capital')
    try:
        rates = ReserveRates(payload.development_rate_bps, payload.safety_rate_bps, payload.owner_rate_bps)
        allocation = rates.allocate(capital)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    wallet_id = f'wallet_{uuid.uuid4().hex[:12]}'
    with closing(connect()) as db:
        db.execute('INSERT INTO wallets (id,total,available,reserved,spent,minimum_liquidity,development_reserve,safety_reserve,owner_reserve,development_rate_bps,safety_rate_bps,owner_rate_bps,created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (wallet_id, money_str(capital), money_str(allocation['operating']), '0.000000', '0.000000', money_str(minimum), money_str(allocation['development']), money_str(allocation['safety']), money_str(allocation['owner']), payload.development_rate_bps, payload.safety_rate_bps, payload.owner_rate_bps, now()))
        ledger(db, wallet_id, 'deposit', capital, metadata={'source': 'initial_capital'})
        for bucket in ('development', 'safety', 'owner'):
            if allocation[bucket]:
                ledger(db, wallet_id, f'allocate_{bucket}', allocation[bucket], metadata={'source': 'deposit_split', 'rate_bps': getattr(payload, f'{bucket}_rate_bps')})
        db.commit()
        return row_dict(db.execute('SELECT * FROM wallets WHERE id = ?', (wallet_id,)).fetchone()) or {}


@app.get('/wallets/{wallet_id}')
def get_wallet(wallet_id: str) -> dict[str, Any]:
    with closing(connect()) as db:
        return row_dict(require_wallet(db, wallet_id)) or {}


@app.post('/wallets/{wallet_id}/deposit', status_code=201)
def deposit_wallet(wallet_id: str, payload: WalletDeposit) -> dict[str, Any]:
    try:
        amount = money(payload.amount)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if amount <= 0:
        raise HTTPException(422, 'deposit amount must be greater than zero')
    with closing(connect()) as db:
        wallet = require_wallet(db, wallet_id)
        rates = ReserveRates(wallet['development_rate_bps'], wallet['safety_rate_bps'], wallet['owner_rate_bps'])
        allocation = rates.allocate(amount)
        db.execute('UPDATE wallets SET total = ?, available = ?, development_reserve = ?, safety_reserve = ?, owner_reserve = ? WHERE id = ?', (money_str(money(wallet['total']) + amount), money_str(money(wallet['available']) + allocation['operating']), money_str(money(wallet['development_reserve']) + allocation['development']), money_str(money(wallet['safety_reserve']) + allocation['safety']), money_str(money(wallet['owner_reserve']) + allocation['owner']), wallet_id))
        ledger(db, wallet_id, 'deposit', amount, metadata={'source': 'deposit'})
        for bucket in ('development', 'safety', 'owner'):
            if allocation[bucket]:
                ledger(db, wallet_id, f'allocate_{bucket}', allocation[bucket], metadata={'source': 'deposit_split', 'rate_bps': wallet[f'{bucket}_rate_bps']})
        db.commit()
        return row_dict(db.execute('SELECT * FROM wallets WHERE id = ?', (wallet_id,)).fetchone()) or {}


@app.post('/upgrades/proposals', status_code=201)
def create_upgrade_proposal(payload: UpgradeProposalCreate) -> dict[str, Any]:
    try:
        cost = money(payload.estimated_cost)
        predicted_value = money(payload.predicted_monthly_value)
        parsed_paths: list[dict[str, Any]] = []
        for raw_path in payload.scenario_paths:
            path = ScenarioSimulator.make_path(str(raw_path['path_id']), str(raw_path['decision_node']), float(raw_path['confidence']), money(raw_path['monetary_exposure']), money(raw_path.get('expected_benefit', '0')))
            parsed_paths.append(path.as_dict())
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f'invalid upgrade proposal: {exc}') from exc
    proposal_id = f'upgrade_{uuid.uuid4().hex[:12]}'
    predicted_roi = float(((predicted_value - cost) / cost).quantize(Decimal('0.0001'))) if cost else 0.0
    with closing(connect()) as db:
        require_wallet(db, payload.wallet_id)
        timestamp = now()
        db.execute('INSERT INTO upgrade_proposals (proposal_id,wallet_id,target_module,estimated_cost,predicted_monthly_value,predicted_roi,actual_roi,status,rdp_metrics,scenario_paths,selected_path,signature_proof,approval_actor,reserved_amount,evidence,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (proposal_id, payload.wallet_id, payload.target_module, money_str(cost), money_str(predicted_value), predicted_roi, None, UpgradeStatus.PENDING_EVALUATION.value, None, json.dumps(parsed_paths, sort_keys=True), None, None, None, '0.000000', '{}', timestamp, timestamp))
        upgrade_event(db, proposal_id, 'PROPOSAL_CREATED', Decimal('0'), {'wallet_id': payload.wallet_id, 'target_module': payload.target_module, 'predicted_roi': predicted_roi})
        db.commit()
        return row_dict(db.execute('SELECT * FROM upgrade_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()) or {}


@app.get('/upgrades/proposals')
def list_upgrade_proposals(status: str | None = None, wallet_id: str | None = None) -> list[dict[str, Any]]:
    with closing(connect()) as db:
        clauses, params = [], []
        if status:
            clauses.append('status = ?'); params.append(status)
        if wallet_id:
            clauses.append('wallet_id = ?'); params.append(wallet_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ''
        return [row_dict(row) or {} for row in db.execute(f'SELECT * FROM upgrade_proposals{where} ORDER BY created_at DESC', params).fetchall()]


@app.get('/upgrades/proposals/{proposal_id}')
def get_upgrade_proposal(proposal_id: str) -> dict[str, Any]:
    with closing(connect()) as db:
        return row_dict(require_upgrade(db, proposal_id)) or {}


@app.post('/upgrades/proposals/{proposal_id}/evaluate')
def evaluate_upgrade_proposal(proposal_id: str, payload: UpgradeEvaluation) -> dict[str, Any]:
    metrics = RDPFitnessMetric(payload.mutation_id, payload.ast_node_coverage, payload.failure_reduction_rate, payload.fitness_score)
    metrics.validate()
    with closing(connect()) as db:
        proposal = require_upgrade(db, proposal_id)
        if proposal['status'] != UpgradeStatus.PENDING_EVALUATION.value:
            raise HTTPException(409, f'proposal is not awaiting evaluation: {proposal["status"]}')
        status = UpgradeStatus.PENDING_HUMAN_GATE if payload.verifier_passed and metrics.fitness_score >= .75 else UpgradeStatus.EVALUATION_FAILED
        db.execute('UPDATE upgrade_proposals SET status = ?, rdp_metrics = ?, evidence = ?, updated_at = ? WHERE proposal_id = ?', (status.value, json.dumps(metrics.as_dict(), sort_keys=True), json.dumps(payload.evidence, sort_keys=True), now(), proposal_id))
        upgrade_event(db, proposal_id, 'EVALUATION_COMPLETED', Decimal('0'), {'status': status.value, 'rdp_metrics': metrics.as_dict(), 'verifier_passed': payload.verifier_passed})
        db.commit()
        return row_dict(db.execute('SELECT * FROM upgrade_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()) or {}


@app.post('/upgrades/proposals/{proposal_id}/promote')
def stage_upgrade_candidate(proposal_id: str, payload: PromotionCreate) -> dict[str, Any]:
    with closing(connect()) as db:
        proposal = require_upgrade(db, proposal_id)
        if proposal['status'] != UpgradeStatus.FUNDS_RESERVED.value:
            raise HTTPException(409, f'proposal must have reserved funds before staging: {proposal["status"]}')
        try:
            destination, receipt = SANDBOX.stage(payload.candidate_id, payload.files)
        except SandboxRejected as exc:
            upgrade_event(db, proposal_id, 'CANDIDATE_REJECTED', Decimal('0'), {'reason': str(exc)})
            db.commit()
            raise HTTPException(422, str(exc)) from exc
        if not receipt.passed:
            upgrade_event(db, proposal_id, 'CANDIDATE_VERIFICATION_FAILED', Decimal('0'), receipt.as_dict())
            db.commit()
            raise HTTPException(422, receipt.reason)
        timestamp = now()
        promotion_id = f'promotion_{uuid.uuid4().hex[:12]}'
        db.execute('INSERT INTO promotion_records (promotion_id,proposal_id,candidate_id,candidate_hash,verification_receipt,canary_percent,status,promotion_receipt,rollback_receipt,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (promotion_id, proposal_id, payload.candidate_id, receipt.candidate_hash, json.dumps(receipt.as_dict(), sort_keys=True), payload.canary_percent, 'STAGED', None, None, timestamp, timestamp))
        db.execute('UPDATE upgrade_proposals SET status = ?, evidence = ?, updated_at = ? WHERE proposal_id = ?', (UpgradeStatus.VERIFICATION_PENDING.value, json.dumps({'candidate_hash': receipt.candidate_hash, 'candidate_path': str(destination), 'verification': receipt.as_dict()}, sort_keys=True), timestamp, proposal_id))
        upgrade_event(db, proposal_id, 'CANDIDATE_STAGED', Decimal('0'), {'promotion_id': promotion_id, 'candidate_hash': receipt.candidate_hash, 'canary_percent': payload.canary_percent})
        db.commit()
        result = row_dict(db.execute('SELECT * FROM promotion_records WHERE promotion_id = ?', (promotion_id,)).fetchone()) or {}
        result['verification'] = receipt.as_dict()
        return result


@app.post('/upgrades/proposals/{proposal_id}/activate')
def activate_upgrade_candidate(proposal_id: str, payload: CanaryActivation) -> dict[str, Any]:
    with closing(connect()) as db:
        proposal = require_upgrade(db, proposal_id)
        promotion = db.execute('SELECT * FROM promotion_records WHERE proposal_id = ?', (proposal_id,)).fetchone()
        if proposal['status'] != UpgradeStatus.VERIFICATION_PENDING.value or not promotion:
            raise HTTPException(409, 'proposal has no staged candidate awaiting canary activation')
        if not payload.canary_passed:
            db.execute('UPDATE promotion_records SET status = ?, updated_at = ? WHERE proposal_id = ?', ('CANARY_FAILED', now(), proposal_id))
            upgrade_event(db, proposal_id, 'CANARY_FAILED', Decimal('0'), {'evidence': payload.evidence})
            db.commit()
            raise HTTPException(422, 'canary verification failed; candidate remains inactive')
        try:
            receipt = SANDBOX.promote(promotion['candidate_id'], promotion['candidate_hash'])
        except SandboxRejected as exc:
            upgrade_event(db, proposal_id, 'PROMOTION_HELD', Decimal('0'), {'reason': str(exc)})
            db.commit()
            raise HTTPException(409, str(exc)) from exc
        timestamp = now()
        db.execute('UPDATE promotion_records SET status = ?, promotion_receipt = ?, updated_at = ? WHERE proposal_id = ?', ('PROMOTED', json.dumps(receipt.as_dict(), sort_keys=True), timestamp, proposal_id))
        db.execute('UPDATE upgrade_proposals SET status = ?, evidence = ?, updated_at = ? WHERE proposal_id = ?', (UpgradeStatus.SETTLED.value, json.dumps({'promotion': receipt.as_dict(), 'canary': payload.evidence}, sort_keys=True), timestamp, proposal_id))
        upgrade_event(db, proposal_id, 'CANDIDATE_PROMOTED', money(proposal['reserved_amount']), {'promotion': receipt.as_dict(), 'canary': payload.evidence})
        db.commit()
        result = row_dict(db.execute('SELECT * FROM promotion_records WHERE proposal_id = ?', (proposal_id,)).fetchone()) or {}
        result['promotion'] = receipt.as_dict()
        return result


@app.post('/upgrades/proposals/{proposal_id}/rollback')
def rollback_upgrade_candidate(proposal_id: str, payload: RollbackRequest) -> dict[str, Any]:
    with closing(connect()) as db:
        proposal = require_upgrade(db, proposal_id)
        promotion = db.execute('SELECT * FROM promotion_records WHERE proposal_id = ?', (proposal_id,)).fetchone()
        if not promotion or proposal['status'] not in {UpgradeStatus.VERIFICATION_PENDING.value, UpgradeStatus.SETTLED.value}:
            raise HTTPException(409, 'proposal has no active or staged candidate to roll back')
        try:
            receipt = SANDBOX.rollback(promotion['candidate_hash'])
        except SandboxRejected as exc:
            upgrade_event(db, proposal_id, 'ROLLBACK_HELD', Decimal('0'), {'reason': str(exc), 'requested_reason': payload.reason})
            db.commit()
            raise HTTPException(409, str(exc)) from exc
        reserved = money(proposal['reserved_amount'])
        wallet = require_wallet(db, proposal['wallet_id'])
        db.execute('UPDATE wallets SET development_reserve = ? WHERE id = ?', (money_str(money(wallet['development_reserve']) + reserved), wallet['id']))
        db.execute('UPDATE promotion_records SET status = ?, rollback_receipt = ?, updated_at = ? WHERE proposal_id = ?', ('ROLLED_BACK', json.dumps(receipt.as_dict(), sort_keys=True), now(), proposal_id))
        db.execute('UPDATE upgrade_proposals SET status = ?, evidence = ?, updated_at = ? WHERE proposal_id = ?', (UpgradeStatus.ROLLED_BACK.value, json.dumps({'rollback': receipt.as_dict(), 'reason': payload.reason}, sort_keys=True), now(), proposal_id))
        ledger(db, wallet['id'], 'upgrade_refund', reserved, metadata={'proposal_id': proposal_id, 'reason': payload.reason, 'promotion_rollback': True})
        upgrade_event(db, proposal_id, 'CANDIDATE_ROLLED_BACK', reserved, {'rollback': receipt.as_dict(), 'reason': payload.reason})
        db.commit()
        result = row_dict(db.execute('SELECT * FROM promotion_records WHERE proposal_id = ?', (proposal_id,)).fetchone()) or {}
        result['rollback'] = receipt.as_dict()
        return result


@app.get('/upgrades/proposals/{proposal_id}/promotion')
def get_upgrade_promotion(proposal_id: str) -> dict[str, Any]:
    with closing(connect()) as db:
        require_upgrade(db, proposal_id)
        promotion = db.execute('SELECT * FROM promotion_records WHERE proposal_id = ?', (proposal_id,)).fetchone()
        if not promotion:
            raise HTTPException(404, 'promotion record not found')
        result = row_dict(promotion) or {}
        result['active'] = SANDBOX.active()
        return result


@app.post('/upgrades/proposals/{proposal_id}/approve')
def approve_upgrade_proposal(proposal_id: str, payload: UpgradeApproval) -> dict[str, Any]:
    public_key = os.environ.get('TREASURY_APPROVAL_PUBLIC_KEY_B64')
    if not public_key:
        raise HTTPException(503, 'TREASURY_APPROVAL_PUBLIC_KEY_B64 is not configured; approval fails closed')
    with closing(connect()) as db:
        proposal = require_upgrade(db, proposal_id)
        if proposal['status'] != UpgradeStatus.PENDING_HUMAN_GATE.value:
            raise HTTPException(409, f'proposal is not awaiting human approval: {proposal["status"]}')
        amount = money(proposal['estimated_cost'])
        approval_payload = canonical_approval_payload(proposal_id, proposal['wallet_id'], amount, payload.actor)
        if not verify_ed25519_signature(public_key, payload.signature_b64, approval_payload):
            raise HTTPException(403, 'invalid Ed25519 approval signature')
        wallet = require_wallet(db, proposal['wallet_id'])
        development = money(wallet['development_reserve'])
        if development < amount:
            raise HTTPException(403, 'insufficient development reserve')
        db.execute('UPDATE wallets SET development_reserve = ? WHERE id = ?', (money_str(development - amount), wallet['id']))
        db.execute('UPDATE upgrade_proposals SET status = ?, signature_proof = ?, approval_actor = ?, reserved_amount = ?, updated_at = ? WHERE proposal_id = ?', (UpgradeStatus.FUNDS_RESERVED.value, payload.signature_b64, payload.actor, money_str(amount), now(), proposal_id))
        ledger(db, wallet['id'], 'upgrade_reserve', amount, metadata={'proposal_id': proposal_id, 'target_module': proposal['target_module'], 'actor': payload.actor})
        upgrade_event(db, proposal_id, 'FUNDS_RESERVED', amount, {'actor': payload.actor, 'signature_verified': True})
        db.commit()
        result = row_dict(db.execute('SELECT * FROM upgrade_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()) or {}
        result['signature_verified'] = True
        return result


@app.post('/upgrades/proposals/{proposal_id}/complete')
def complete_upgrade_proposal(proposal_id: str, payload: UpgradeCompletion) -> dict[str, Any]:
    with closing(connect()) as db:
        proposal = require_upgrade(db, proposal_id)
        if proposal['status'] != UpgradeStatus.FUNDS_RESERVED.value:
            raise HTTPException(409, f'proposal is not funded: {proposal["status"]}')
        actual_value = money(payload.actual_monthly_value)
        cost = money(proposal['estimated_cost'])
        actual_roi = float(((actual_value - cost) / cost).quantize(Decimal('0.0001'))) if cost else 0.0
        wallet = require_wallet(db, proposal['wallet_id'])
        reserved = money(proposal['reserved_amount'])
        if payload.verified:
            status = UpgradeStatus.SETTLED
            ledger(db, wallet['id'], 'upgrade_settle', reserved, metadata={'proposal_id': proposal_id, 'actual_monthly_value': money_str(actual_value), 'actual_roi': actual_roi, 'evidence': payload.evidence})
            upgrade_event(db, proposal_id, 'UPGRADE_SETTLED', reserved, {'actual_roi': actual_roi, 'evidence': payload.evidence})
        else:
            status = UpgradeStatus.ROLLED_BACK
            db.execute('UPDATE wallets SET development_reserve = ? WHERE id = ?', (money_str(money(wallet['development_reserve']) + reserved), wallet['id']))
            ledger(db, wallet['id'], 'upgrade_refund', reserved, metadata={'proposal_id': proposal_id, 'reason': 'verification_failed', 'evidence': payload.evidence})
            upgrade_event(db, proposal_id, 'UPGRADE_ROLLED_BACK', reserved, {'actual_roi': actual_roi, 'evidence': payload.evidence})
        db.execute('UPDATE upgrade_proposals SET status = ?, actual_roi = ?, evidence = ?, updated_at = ? WHERE proposal_id = ?', (status.value, actual_roi, json.dumps(payload.evidence, sort_keys=True), now(), proposal_id))
        db.commit()
        return row_dict(db.execute('SELECT * FROM upgrade_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()) or {}


@app.get('/upgrades/events')
def list_upgrade_events() -> dict[str, Any]:
    with closing(connect()) as db:
        return {'ledger_verified': verify_upgrade_events(db), 'events': [row_dict(row) or {} for row in db.execute('SELECT * FROM upgrade_events ORDER BY sequence ASC').fetchall()]}


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


@app.get('/hf/models')
def list_huggingface_models(capability: str | None = None) -> dict[str, Any]:
    models = hf_catalog(capability)
    return {
        'source': 'huggingface_inference_providers',
        'routing_policies': ['cheapest', 'fastest', 'preferred'],
        'models': [model.as_dict() for model in models],
        'note': 'Rates are blocked until HF_MODEL_PRICING_JSON contains verified operator pricing.' if not any(model.pricing_verified for model in models) else 'Pricing is configured from operator-supplied rates; confirm against the active provider before production use.',
    }


@app.get('/hf/provider-status')
def huggingface_provider_status() -> dict[str, Any]:
    models = hf_catalog()
    return {
        'provider': 'huggingface_inference_providers',
        'endpoint': HuggingFaceChatAdapter.endpoint,
        'token_configured': bool(os.environ.get('HF_TOKEN')),
        'verified_priced_models': sum(model.pricing_verified for model in models),
        'catalog_size': len(models),
        'execution_status': 'ready' if os.environ.get('HF_TOKEN') and any(model.pricing_verified for model in models) else 'fail_closed_configuration_required',
        'secrets_are_server_side': True,
    }


@app.post('/hf/quote')
def quote_huggingface_model(payload: HuggingFaceQuoteRequest) -> dict[str, Any]:
    try:
        maximum = money(payload.max_cost)
        selected, estimated_cost, alternatives = hf_select_model(capabilities=payload.capabilities, input_tokens=payload.input_tokens, output_tokens=payload.output_tokens, max_cost=maximum, minimum_quality=payload.minimum_quality, routing_policy=payload.routing_policy, preferred_providers=payload.preferred_providers, model_id=payload.model_id)
    except (ValueError, ModelPolicyViolation) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        'status': 'QUOTED',
        'selected_model': selected.as_dict(),
        'routing_policy': payload.routing_policy,
        'estimated_cost': money_str(estimated_cost),
        'input_tokens': payload.input_tokens,
        'output_tokens': payload.output_tokens,
        'alternatives': alternatives,
        'execution_authorized': False,
        'wallet_reservation_required': True,
        'note': 'A quote is not an execution authorization. Reserve and settle through a governed mission before calling a provider.',
    }


@app.post('/hf/chat')
def huggingface_chat(payload: HuggingFaceChatRequest) -> dict[str, Any]:
    if os.environ.get('HF_ALLOW_UNSETTLED_CHAT') != 'true':
        raise HTTPException(403, 'unsettled provider calls are disabled; integrate wallet reservation and settlement before enabling HF_ALLOW_UNSETTLED_CHAT')
    try:
        maximum = money(payload.max_cost)
        selected, estimated_cost, alternatives = hf_select_model(capabilities=payload.capabilities, input_tokens=payload.input_tokens, output_tokens=payload.output_tokens, max_cost=maximum, minimum_quality=payload.minimum_quality, routing_policy=payload.routing_policy, preferred_providers=payload.preferred_providers, model_id=payload.model_id)
    except (ValueError, ModelPolicyViolation) as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        result = HuggingFaceChatAdapter().chat(selected, payload.messages, routing_policy=payload.routing_policy, provider=payload.provider, max_tokens=payload.max_tokens)
    except HuggingFaceProviderUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
    input_tokens = int(usage.get('prompt_tokens', payload.input_tokens))
    output_tokens = int(usage.get('completion_tokens', payload.output_tokens))
    try:
        actual_cost = hf_quote_model(selected, input_tokens, output_tokens)
    except ModelPolicyViolation as exc:
        raise HTTPException(422, str(exc)) from exc
    if actual_cost > maximum:
        raise HTTPException(502, 'provider usage exceeded the quoted economic envelope; no settlement was recorded')
    return {
        'status': 'PROVIDER_RESPONSE',
        'selected_model': selected.as_dict(),
        'routing_policy': payload.routing_policy,
        'estimated_cost': money_str(estimated_cost),
        'actual_cost': money_str(actual_cost),
        'usage': usage,
        'latency_ms': result.get('latency_ms'),
        'response': result.get('message'),
        'alternatives': alternatives,
        'execution_authorized': False,
        'settlement_required': True,
        'note': 'Provider response returned for a governed executor; wallet reservation and settlement remain a separate required step.',
    }


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


@app.post('/capture', status_code=201)
def capture_thought(payload: ThoughtCaptureRequest) -> dict[str, Any]:
    """Capture a thought and create a bounded, non-executing mission envelope."""
    try:
        max_cost = money(payload.max_cost)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if max_cost <= 0:
        raise HTTPException(422, 'max_cost must be greater than zero')
    runtime_policy = {'local_only': 'local', 'user_server': 'user_server', 'approved_external': 'approved_external'}[payload.privacy_mode]
    memory = MEMORY.remember(payload.content, memory_type='thought_capture', source_id='capture', actor=payload.actor)
    mission_id = f'mission_{uuid.uuid4().hex[:12]}'
    envelope_id = f'envelope_{uuid.uuid4().hex[:12]}'
    idempotency_key = hashlib.sha256(f'{memory["content_hash"]}:{payload.wallet_id}:{payload.task_type}'.encode()).hexdigest()
    quote_body = {'capabilities': sorted(payload.capabilities), 'max_cost': money_str(max_cost), 'minimum_quality': payload.minimum_quality, 'privacy_mode': payload.privacy_mode, 'task_type': payload.task_type}
    quote_hash = hashlib.sha256(json.dumps(quote_body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    created_at = now()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with closing(connect()) as db:
        wallet = require_wallet(db, payload.wallet_id)
        if payload.project_id:
            require_project(db, payload.project_id)
        existing = db.execute('SELECT * FROM execution_envelopes WHERE idempotency_key = ?', (idempotency_key,)).fetchone()
        if existing:
            mission = db.execute('SELECT * FROM missions WHERE id = ?', (existing['mission_id'],)).fetchone()
            return {'memory': memory, 'mission': row_dict(mission) or {}, 'envelope': row_dict(existing) or {}, 'idempotent_replay': True, 'execution_authorized': False, 'escrow_reserved': False}
        db.execute('INSERT INTO missions (id, wallet_id, description, max_cost, minimum_quality, task_type, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (mission_id, wallet['id'], payload.content, money_str(max_cost), payload.minimum_quality, payload.task_type, 'planned', created_at))
        db.execute('INSERT INTO execution_envelopes (envelope_id,mission_id,memory_id,wallet_id,privacy_mode,runtime_policy,task_type,max_cost,minimum_quality,capabilities,quote_hash,idempotency_key,status,expires_at,created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (envelope_id, mission_id, memory['memory_id'], wallet['id'], payload.privacy_mode, runtime_policy, payload.task_type, money_str(max_cost), payload.minimum_quality, json.dumps(sorted(payload.capabilities)), quote_hash, idempotency_key, 'AWAITING_EXECUTION', expires_at, created_at))
        db.commit()
        envelope = row_dict(db.execute('SELECT * FROM execution_envelopes WHERE envelope_id = ?', (envelope_id,)).fetchone()) or {}
        mission = row_dict(db.execute('SELECT * FROM missions WHERE id = ?', (mission_id,)).fetchone()) or {}
        return {'memory': memory, 'mission': mission, 'envelope': envelope, 'execution_authorized': False, 'escrow_reserved': False, 'note': 'Thought captured. No model or provider was called; execution requires a governed reservation and an eligible executor.'}


@app.get('/capture/{envelope_id}')
def get_capture(envelope_id: str) -> dict[str, Any]:
    with closing(connect()) as db:
        envelope = db.execute('SELECT * FROM execution_envelopes WHERE envelope_id = ?', (envelope_id,)).fetchone()
        if not envelope:
            raise HTTPException(404, 'capture envelope not found')
        mission = db.execute('SELECT * FROM missions WHERE id = ?', (envelope['mission_id'],)).fetchone()
        return {'memory': MEMORY.get(envelope['memory_id']), 'mission': row_dict(mission) or {}, 'envelope': row_dict(envelope) or {}, 'execution_authorized': False, 'escrow_reserved': False}


@app.get('/local-executor/status')
def local_executor_status() -> dict[str, Any]:
    try:
        return LocalCommandExecutor().status()
    except LocalExecutorUnavailable as exc:
        return {'configured': False, 'runtime': 'local_command', 'isolation': 'process_boundary_only', 'error': str(exc)}


@app.get('/local-models')
def local_models() -> dict[str, Any]:
    return local_model_status()


@app.post('/capture/{envelope_id}/execute-local')
def execute_capture_locally(envelope_id: str, payload: LocalExecuteRequest) -> dict[str, Any]:
    try:
        adapter = select_local_model(payload.model_id)
    except (LocalExecutorUnavailable, LocalModelUnavailable) as exc:
        raise HTTPException(503, str(exc)) from exc
    with closing(connect()) as db:
        prior = db.execute('SELECT * FROM local_executions WHERE execution_id = ?', (payload.execution_id,)).fetchone()
        if prior:
            return {'execution_id': payload.execution_id, 'envelope_id': prior['envelope_id'], 'status': prior['status'], 'output': prior['output'], 'stderr': prior['stderr'], 'usage': json.loads(prior['usage']), 'amount': prior['amount'], 'idempotent_replay': True, 'execution_authorized': True}
        envelope = db.execute('SELECT * FROM execution_envelopes WHERE envelope_id = ?', (envelope_id,)).fetchone()
        if not envelope:
            raise HTTPException(404, 'capture envelope not found')
        if envelope['privacy_mode'] != 'local_only':
            raise HTTPException(403, 'local executor is only eligible for local_only envelopes')
        if envelope['status'] != 'AWAITING_EXECUTION':
            raise HTTPException(409, f'envelope is not executable from status {envelope["status"]}')
        mission = db.execute('SELECT * FROM missions WHERE id = ?', (envelope['mission_id'],)).fetchone()
        wallet = require_wallet(db, envelope['wallet_id'])
        reservation_amount = money(envelope['max_cost'])
        available = money(wallet['available'])
        minimum = money(wallet['minimum_liquidity'])
        if available - reservation_amount < minimum:
            raise HTTPException(403, 'reservation would violate minimum liquidity protection')
        reservation_id = f'local_reservation_{uuid.uuid4().hex[:12]}'
        db.execute('INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?)', (reservation_id, wallet['id'], mission['id'], money_str(reservation_amount), 'reserved', now(), None))
        db.execute('UPDATE wallets SET available = ?, reserved = ? WHERE id = ?', (money_str(available - reservation_amount), money_str(money(wallet['reserved']) + reservation_amount), wallet['id']))
        ledger(db, wallet['id'], 'reserve', reservation_amount, mission_id=mission['id'], reservation_id=reservation_id, metadata={'execution_id': payload.execution_id, 'runtime': 'local'})
        db.commit()
        memory = MEMORY.get(envelope['memory_id'])
        prompt = memory['content'] if memory else ''
    try:
        result = adapter.execute(prompt, payload.timeout_seconds)
    except LocalModelUnavailable as exc:
        with closing(connect()) as db:
            wallet = require_wallet(db, envelope['wallet_id'])
            db.execute('UPDATE reservations SET status = ?, settled_at = ? WHERE id = ?', ('refunded', now(), reservation_id))
            db.execute('UPDATE wallets SET available = ?, reserved = ? WHERE id = ?', (money_str(money(wallet['available']) + reservation_amount), money_str(money(wallet['reserved']) - reservation_amount), wallet['id']))
            ledger(db, wallet['id'], 'release', reservation_amount, mission_id=envelope['mission_id'], reservation_id=reservation_id, metadata={'execution_id': payload.execution_id, 'runtime': 'local', 'reason': str(exc)})
            db.commit()
        raise HTTPException(502, str(exc)) from exc
    usage_obj = ResourceUsage(Decimal(str(result.duration_seconds)), 0, result.input_bytes, result.output_bytes, result.token_count, Decimal(str(result.duration_seconds)))
    actual = money(PricingPolicy().price(usage_obj)) if result.status == 'SUCCESS' else Decimal('0.000000')
    success = result.status == 'SUCCESS' and actual <= reservation_amount
    if result.status == 'SUCCESS' and actual > reservation_amount:
        result = result.__class__(result.status, result.output, 'measured cost exceeded the execution envelope', result.duration_seconds, result.input_bytes, result.output_bytes, result.token_count)
    with closing(connect()) as db:
        wallet = require_wallet(db, envelope['wallet_id'])
        unused = reservation_amount - actual
        db.execute('UPDATE reservations SET status = ?, settled_at = ? WHERE id = ?', ('settled' if success else 'refunded', now(), reservation_id))
        db.execute('UPDATE wallets SET available = ?, reserved = ?, spent = ? WHERE id = ?', (money_str(money(wallet['available']) + unused), money_str(money(wallet['reserved']) - reservation_amount), money_str(money(wallet['spent']) + actual), wallet['id']))
        event = 'outcome' if success else 'failure'
        ledger(db, wallet['id'], event, actual, mission_id=envelope['mission_id'], reservation_id=reservation_id, metadata={'execution_id': payload.execution_id, 'runtime': 'local', 'usage': usage_obj.as_dict(), 'status': result.status})
        if unused:
            ledger(db, wallet['id'], 'release', unused, mission_id=envelope['mission_id'], reservation_id=reservation_id, metadata={'execution_id': payload.execution_id, 'runtime': 'local'})
        final_status = 'COMPLETED' if success else 'FAILED'
        db.execute('UPDATE missions SET status = ? WHERE id = ?', ('completed' if success else 'failed', envelope['mission_id']))
        db.execute('UPDATE execution_envelopes SET status = ? WHERE envelope_id = ?', (final_status, envelope_id))
        db.execute('INSERT INTO local_executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (payload.execution_id, envelope_id, envelope['mission_id'], reservation_id, 'SETTLED' if success else 'REFUNDED', result.output, result.stderr, json.dumps({'model_id': result.model_id, 'runtime': result.runtime, **usage_obj.as_dict()}, sort_keys=True), money_str(actual), now()))
        db.commit()
        final_wallet = row_dict(db.execute('SELECT * FROM wallets WHERE id = ?', (wallet['id'],)).fetchone()) or {}
        return {'execution_id': payload.execution_id, 'envelope_id': envelope_id, 'model_id': result.model_id, 'runtime': result.runtime, 'status': final_status, 'output': result.output, 'stderr': result.stderr, 'usage': usage_obj.as_dict(), 'amount': money_str(actual), 'wallet': final_wallet, 'execution_authorized': True, 'escrow_reserved': False, 'note': 'Local model execution completed. Runtime isolation depends on the selected adapter; the command adapter is process-boundary only.'}


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
