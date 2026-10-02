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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from app.software_factory_pipeline import FactoryPipelineRequest, SoftwareFactoryPipeline
from app.substrate import PricingPolicy, ResourceUsage
from app.memory import MemoryAPI
from app.micro_billing import MetricType, SplitRevenueContract, SplitRule, SynapseMicroBillingEngine, TelemetryEvent

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(__import__('os').environ.get('SOVEREIGN_DB', BASE_DIR / 'sovereign.db'))

app = FastAPI(title='Sovereign Economic Engine', version='0.1.0')
MICRO_BILLING = SynapseMicroBillingEngine()
MEMORY = MemoryAPI(__import__('os').environ.get('SOVEREIGN_MEMORY_DB', BASE_DIR / 'sovereign-memory.db'))
FACTORY_LEDGER_PATH = Path(__import__('os').environ.get('SOVEREIGN_FACTORY_LEDGER', str(DB_PATH.with_name('sail_event_spine.jsonl'))))


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


def get_factory_pipeline() -> SoftwareFactoryPipeline:
    return SoftwareFactoryPipeline(str(FACTORY_LEDGER_PATH))


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
        CREATE TABLE IF NOT EXISTS skills (
            skill_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            metadata TEXT NOT NULL,
            lineage TEXT NOT NULL,
            provenance TEXT NOT NULL,
            evidence TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS skill_events (
            event_id TEXT PRIMARY KEY,
            skill_id TEXT NOT NULL REFERENCES skills(skill_id),
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            governance_proof TEXT,
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


class SkillCandidateCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: str = Field(default='candidate', pattern=r'^(discovered|candidate)$')
    metadata: dict[str, Any] = Field(default_factory=dict)
    lineage: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class SkillQualificationRequest(BaseModel):
    tool_code: str = Field(min_length=1)
    test_code: str = Field(min_length=1)
    target_file: str = Field(min_length=1)
    timeout_seconds: int = Field(default=5, ge=1, le=60)
    memory_limit_mb: int = Field(default=256, ge=64, le=4096)


class SkillAdmissionRequest(BaseModel):
    governance_proposal_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    note: str = ''


class SkillRetirementRequest(BaseModel):
    governance_proposal_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EconomicMissionRequest(BaseModel):
    wallet_id: str
    task_intent: str = Field(min_length=1)
    budget: str = Field(pattern=r'^\d+(\.\d{1,6})?$')
    minimum_quality: float = Field(ge=0, le=1)
    task_type: str = 'general'
    required_model_id: str | None = None
    model_requirements: list[str] = Field(default_factory=list)
    resource_constraints: dict[str, Any] = Field(default_factory=dict)
    quality_constraints: dict[str, Any] = Field(default_factory=dict)
    execution_id: str | None = None
    cpu_seconds: float = Field(default=0, ge=0)
    peak_memory_bytes: int = Field(default=0, ge=0)
    io_read_bytes: int = Field(default=0, ge=0)
    io_write_bytes: int = Field(default=0, ge=0)
    token_count: int = Field(default=0, ge=0)
    wall_seconds: float = Field(default=0, ge=0)
    outcome: str = Field(default='verified_success', pattern=r'^(verified_success|failed)$')
    evidence: dict[str, Any] = Field(default_factory=dict)


class DemoRunRequest(BaseModel):
    capital: str = Field(default='10.00', pattern=r'^\d+(\.\d{1,6})?$')
    minimum_liquidity: str = Field(default='1.00', pattern=r'^\d+(\.\d{1,6})?$')
    budget: str = Field(default='1.00', pattern=r'^\d+(\.\d{1,6})?$')
    minimum_quality: float = Field(default=0.8, ge=0, le=1)
    task_intent: str = Field(default='Demo governed mission')
    task_type: str = Field(default='general')
    token_count: int = Field(default=420000, ge=0)


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


def require_skill(db: sqlite3.Connection, skill_id: str) -> sqlite3.Row:
    skill = db.execute('SELECT * FROM skills WHERE skill_id = ?', (skill_id,)).fetchone()
    if not skill:
        raise HTTPException(404, 'skill not found')
    return skill


def decode_json_fields(entry: dict[str, Any], *fields: str) -> dict[str, Any]:
    decoded = dict(entry)
    for field in fields:
        if field in decoded and isinstance(decoded[field], str):
            try:
                decoded[field] = json.loads(decoded[field])
            except json.JSONDecodeError:
                pass
    return decoded


def append_skill_event(db: sqlite3.Connection, skill_id: str, event_type: str, payload: dict[str, Any], governance_proof: str | None = None) -> None:
    db.execute('INSERT INTO skill_events VALUES (?, ?, ?, ?, ?, ?)', (f'skill_event_{uuid.uuid4().hex[:18]}', skill_id, event_type, json.dumps(payload, sort_keys=True), governance_proof, now()))


def require_human_governance_proof(db: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    proposal = db.execute('SELECT * FROM pocket_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()
    if not proposal:
        raise HTTPException(422, 'governance proposal not found')
    if proposal['status'] not in {'USER_APPROVED_PENDING_EXTERNAL_PURCHASE', 'TRIAL_EXTENDED', 'REJECTED'}:
        raise HTTPException(409, 'governance proposal is not human-ratified')
    return proposal


@app.get('/health')
def health() -> dict[str, Any]:
    memory_health = MEMORY.health()
    with closing(connect()) as db:
        pocket_ledger_verified = verify_pocket_events(db)
    return {
        'status': 'ok' if memory_health['ledger_verified'] and pocket_ledger_verified else 'degraded',
        'service': 'sovereign-economic-engine',
        'components': {
            'economic_wallet': 'ready',
            'micro_billing': 'ready_in_process_memory',
            'memory': memory_health,
            'pocket_os': {'status': 'ok', 'event_ledger_verified': pocket_ledger_verified},
            'external_model_executor': 'not_connected',
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


@app.get('/', response_class=HTMLResponse)
@app.get('/console', response_class=HTMLResponse)
def control_console() -> str:
    console_path = BASE_DIR / 'app' / 'static' / 'control_console.html'
    if not console_path.exists():
        raise HTTPException(500, 'control console asset missing')
    return console_path.read_text(encoding='utf-8')


@app.get('/wallets')
def list_wallets() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [row_dict(row) or {} for row in db.execute('SELECT * FROM wallets ORDER BY created_at DESC').fetchall()]


@app.get('/missions')
def list_missions() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [row_dict(row) or {} for row in db.execute('SELECT * FROM missions ORDER BY created_at DESC').fetchall()]


@app.get('/routing-decisions')
def list_routing_decisions() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        rows = db.execute('SELECT * FROM routing_decisions ORDER BY created_at DESC').fetchall()
        return [decode_json_fields(row_dict(row) or {}, 'decision', 'alternatives') for row in rows]


@app.get('/reservations')
def list_reservations() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [row_dict(row) or {} for row in db.execute('SELECT * FROM reservations ORDER BY created_at DESC').fetchall()]


@app.get('/executions')
def list_executions() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        rows = db.execute('SELECT * FROM executions ORDER BY created_at DESC').fetchall()
        return [decode_json_fields(row_dict(row) or {}, 'usage', 'evidence') for row in rows]


@app.get('/settlements')
def list_settlements() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [row_dict(row) or {} for row in db.execute('SELECT * FROM settlements ORDER BY created_at DESC').fetchall()]


@app.post('/skills/candidates', status_code=201)
def create_skill_candidate(payload: SkillCandidateCreate) -> dict[str, Any]:
    skill_id = f'skill_{uuid.uuid4().hex[:12]}'
    created = now()
    with closing(connect()) as db:
        db.execute(
            'INSERT INTO skills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                skill_id,
                payload.name,
                payload.description,
                payload.status,
                json.dumps(payload.metadata, sort_keys=True),
                json.dumps(payload.lineage, sort_keys=True),
                json.dumps(payload.provenance, sort_keys=True),
                json.dumps(payload.evidence, sort_keys=True),
                created,
                created,
            ),
        )
        append_skill_event(db, skill_id, 'SKILL_REGISTERED', payload.model_dump())
        db.commit()
        skill = row_dict(db.execute('SELECT * FROM skills WHERE skill_id = ?', (skill_id,)).fetchone()) or {}
        return decode_json_fields(skill, 'metadata', 'lineage', 'provenance', 'evidence')


@app.get('/skills')
def list_skills(status: str | None = None) -> list[dict[str, Any]]:
    with closing(connect()) as db:
        if status:
            rows = db.execute('SELECT * FROM skills WHERE status = ? ORDER BY updated_at DESC', (status,)).fetchall()
        else:
            rows = db.execute('SELECT * FROM skills ORDER BY updated_at DESC').fetchall()
        return [decode_json_fields(row_dict(row) or {}, 'metadata', 'lineage', 'provenance', 'evidence') for row in rows]


@app.get('/skills/{skill_id}')
def get_skill(skill_id: str) -> dict[str, Any]:
    with closing(connect()) as db:
        skill = decode_json_fields(row_dict(require_skill(db, skill_id)) or {}, 'metadata', 'lineage', 'provenance', 'evidence')
        events = db.execute('SELECT * FROM skill_events WHERE skill_id = ? ORDER BY created_at ASC', (skill_id,)).fetchall()
        skill['events'] = [decode_json_fields(row_dict(event) or {}, 'payload') for event in events]
        return skill


@app.post('/skills/{skill_id}/qualify')
def qualify_skill(skill_id: str, payload: SkillQualificationRequest) -> dict[str, Any]:
    with closing(connect()) as db:
        skill = require_skill(db, skill_id)
        if skill['status'] not in {'candidate', 'discovered'}:
            raise HTTPException(409, f'skill cannot be qualified from status {skill["status"]}')
        evaluation = get_factory_pipeline().execute_pipeline(
            FactoryPipelineRequest(
                tool_code=payload.tool_code,
                test_code=payload.test_code,
                target_file=payload.target_file,
                timeout_seconds=payload.timeout_seconds,
                memory_limit_mb=payload.memory_limit_mb,
            )
        )
        evidence = json.loads(skill['evidence'])
        evidence.append(
            {
                'evaluation_status': evaluation.status,
                'patch_hash': evaluation.patch_hash,
                'ast_report': evaluation.ast_report,
                'sandbox_report': evaluation.sandbox_report,
                'ledger_hash': evaluation.ledger_hash,
                'timestamp': evaluation.timestamp,
            }
        )
        next_status = 'qualified' if evaluation.status == 'PROMOTED' else skill['status']
        db.execute('UPDATE skills SET status = ?, evidence = ?, updated_at = ? WHERE skill_id = ?', (next_status, json.dumps(evidence, sort_keys=True), now(), skill_id))
        append_skill_event(
            db,
            skill_id,
            'SKILL_QUALIFIED' if next_status == 'qualified' else 'SKILL_EVALUATED_REJECTED',
            {'result': evaluation.model_dump()},
        )
        db.commit()
        updated = row_dict(db.execute('SELECT * FROM skills WHERE skill_id = ?', (skill_id,)).fetchone()) or {}
        return decode_json_fields(updated, 'metadata', 'lineage', 'provenance', 'evidence')


@app.post('/skills/{skill_id}/admit')
def admit_skill(skill_id: str, payload: SkillAdmissionRequest) -> dict[str, Any]:
    with closing(connect()) as db:
        skill = require_skill(db, skill_id)
        if skill['status'] != 'qualified':
            raise HTTPException(409, 'only a qualified skill can be admitted')
        proposal = require_human_governance_proof(db, payload.governance_proposal_id)
        if proposal['status'] != 'USER_APPROVED_PENDING_EXTERNAL_PURCHASE':
            raise HTTPException(409, 'skill admission requires approved governance proof')
        db.execute('UPDATE skills SET status = ?, updated_at = ? WHERE skill_id = ?', ('admitted', now(), skill_id))
        append_skill_event(db, skill_id, 'SKILL_ADMITTED', {'actor': payload.actor, 'note': payload.note}, payload.governance_proposal_id)
        db.commit()
        updated = row_dict(db.execute('SELECT * FROM skills WHERE skill_id = ?', (skill_id,)).fetchone()) or {}
        return decode_json_fields(updated, 'metadata', 'lineage', 'provenance', 'evidence')


@app.post('/skills/{skill_id}/retire')
def retire_skill(skill_id: str, payload: SkillRetirementRequest) -> dict[str, Any]:
    with closing(connect()) as db:
        skill = require_skill(db, skill_id)
        if skill['status'] not in {'admitted', 'qualified'}:
            raise HTTPException(409, f'skill cannot be retired from status {skill["status"]}')
        require_human_governance_proof(db, payload.governance_proposal_id)
        db.execute('UPDATE skills SET status = ?, updated_at = ? WHERE skill_id = ?', ('retired', now(), skill_id))
        append_skill_event(db, skill_id, 'SKILL_RETIRED', {'actor': payload.actor, 'reason': payload.reason}, payload.governance_proposal_id)
        db.commit()
        updated = row_dict(db.execute('SELECT * FROM skills WHERE skill_id = ?', (skill_id,)).fetchone()) or {}
        return decode_json_fields(updated, 'metadata', 'lineage', 'provenance', 'evidence')


@app.post('/console/missions/execute', status_code=201)
def create_and_execute_mission(payload: EconomicMissionRequest) -> dict[str, Any]:
    mission = create_mission(
        MissionCreate(
            wallet_id=payload.wallet_id,
            description=payload.task_intent,
            max_cost=payload.budget,
            minimum_quality=payload.minimum_quality,
            task_type=payload.task_type,
        )
    )
    decision = decide_route(mission['id'])
    execution_evidence = {
        'model_requirements': payload.model_requirements,
        'resource_constraints': payload.resource_constraints,
        'quality_constraints': payload.quality_constraints,
        **payload.evidence,
    }
    if decision['status'] == 'HOLD':
        return {
            'mission': mission,
            'routing': decision,
            'status': 'HOLD',
            'reason': decision.get('reason'),
            'evidence': execution_evidence,
        }
    selected_model = decision.get('selected_model')
    if payload.required_model_id and payload.required_model_id != selected_model:
        return {
            'mission': mission,
            'routing': decision,
            'status': 'HOLD',
            'reason': 'ROUTED_MODEL_DOES_NOT_MATCH_REQUIRED_MODEL',
            'required_model_id': payload.required_model_id,
            'selected_model': selected_model,
            'evidence': execution_evidence,
        }
    execution = execute(
        ExecuteRequest(
            mission_id=mission['id'],
            model_id=payload.required_model_id or selected_model,
            execution_id=payload.execution_id or f'exec_{uuid.uuid4().hex[:14]}',
            cpu_seconds=payload.cpu_seconds,
            peak_memory_bytes=payload.peak_memory_bytes,
            io_read_bytes=payload.io_read_bytes,
            io_write_bytes=payload.io_write_bytes,
            token_count=payload.token_count,
            wall_seconds=payload.wall_seconds,
            outcome=payload.outcome,
            evidence=execution_evidence,
        )
    )
    return {'mission': mission, 'routing': decision, 'execution': execution, 'status': execution['outcome']}


@app.post('/console/demo/run', status_code=201)
def run_demo(payload: DemoRunRequest) -> dict[str, Any]:
    wallet = create_wallet(WalletCreate(capital=payload.capital, minimum_liquidity=payload.minimum_liquidity))
    model = register_model(
        ModelCreate(
            name='Demo Model',
            task_types=[payload.task_type],
            quality=0.95,
            reliability=0.95,
            cost_per_task='0.50',
            latency_ms=800,
        )
    )
    run = create_and_execute_mission(
        EconomicMissionRequest(
            wallet_id=wallet['id'],
            task_intent=payload.task_intent,
            budget=payload.budget,
            minimum_quality=payload.minimum_quality,
            task_type=payload.task_type,
            required_model_id=model['id'],
            token_count=payload.token_count,
            evidence={'mode': 'demo_simulation'},
        )
    )
    return {'wallet': wallet, 'model': model, 'run': run}


@app.get('/console/observability')
def console_observability() -> dict[str, Any]:
    with closing(connect()) as db:
        wallets = [row_dict(row) or {} for row in db.execute('SELECT * FROM wallets ORDER BY created_at DESC').fetchall()]
        missions = [row_dict(row) or {} for row in db.execute('SELECT * FROM missions ORDER BY created_at DESC').fetchall()]
        decisions = [decode_json_fields(row_dict(row) or {}, 'decision', 'alternatives') for row in db.execute('SELECT * FROM routing_decisions ORDER BY created_at DESC').fetchall()]
        reservations = [row_dict(row) or {} for row in db.execute('SELECT * FROM reservations ORDER BY created_at DESC').fetchall()]
        executions = [decode_json_fields(row_dict(row) or {}, 'usage', 'evidence') for row in db.execute('SELECT * FROM executions ORDER BY created_at DESC').fetchall()]
        settlements = [row_dict(row) or {} for row in db.execute('SELECT * FROM settlements ORDER BY created_at DESC').fetchall()]
        ledger_rows = [decode_json_fields(row_dict(row) or {}, 'metadata') for row in db.execute('SELECT * FROM ledger ORDER BY created_at DESC').fetchall()]
        skill_rows = [decode_json_fields(row_dict(row) or {}, 'metadata', 'lineage', 'provenance', 'evidence') for row in db.execute('SELECT * FROM skills ORDER BY updated_at DESC').fetchall()]
        skill_events = [decode_json_fields(row_dict(row) or {}, 'payload') for row in db.execute('SELECT * FROM skill_events ORDER BY created_at DESC').fetchall()]
        pocket_events = [decode_json_fields(row_dict(row) or {}, 'payload') for row in db.execute('SELECT * FROM pocket_events ORDER BY sequence DESC').fetchall()]
        return {
            'wallets': wallets,
            'missions': missions,
            'routing_decisions': decisions,
            'reservations': reservations,
            'executions': executions,
            'settlements': settlements,
            'ledger': ledger_rows,
            'skills': skill_rows,
            'skill_events': skill_events,
            'pocket_events': pocket_events,
        }


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
