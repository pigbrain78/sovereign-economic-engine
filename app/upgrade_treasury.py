from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature


class UpgradeStatus(StrEnum):
    PENDING_EVALUATION = 'PENDING_EVALUATION'
    EVALUATION_FAILED = 'EVALUATION_FAILED'
    PENDING_HUMAN_GATE = 'PENDING_HUMAN_GATE'
    FUNDS_RESERVED = 'FUNDS_RESERVED'
    VERIFICATION_PENDING = 'VERIFICATION_PENDING'
    SETTLED = 'SETTLED'
    ROLLED_BACK = 'ROLLED_BACK'
    REJECTED = 'REJECTED'


@dataclass(frozen=True)
class ReserveRates:
    development_bps: int = 1000
    safety_bps: int = 500
    owner_bps: int = 500

    def validate(self) -> None:
        values = (self.development_bps, self.safety_bps, self.owner_bps)
        if any(value < 0 or value > 10000 for value in values) or sum(values) > 10000:
            raise ValueError('reserve rates must be between 0 and 10000 bps and sum to at most 10000')

    def allocate(self, amount: Decimal) -> dict[str, Decimal]:
        self.validate()
        if amount < 0:
            raise ValueError('allocation amount cannot be negative')
        scale = Decimal(10000)
        development = (amount * self.development_bps / scale).quantize(Decimal('0.000001'), rounding=ROUND_DOWN)
        safety = (amount * self.safety_bps / scale).quantize(Decimal('0.000001'), rounding=ROUND_DOWN)
        owner = (amount * self.owner_bps / scale).quantize(Decimal('0.000001'), rounding=ROUND_DOWN)
        operating = amount - development - safety - owner
        return {'operating': operating, 'development': development, 'safety': safety, 'owner': owner}


@dataclass(frozen=True)
class RDPFitnessMetric:
    mutation_id: str
    ast_node_coverage: float
    failure_reduction_rate: float
    fitness_score: float

    def validate(self) -> None:
        if not self.mutation_id or not 0 <= self.ast_node_coverage <= 1 or not 0 <= self.failure_reduction_rate <= 1 or not 0 <= self.fitness_score <= 1:
            raise ValueError('RDP metrics are outside their allowed ranges')

    def as_dict(self) -> dict[str, Any]:
        return {'mutation_id': self.mutation_id, 'ast_node_coverage': self.ast_node_coverage, 'failure_reduction_rate': self.failure_reduction_rate, 'fitness_score': self.fitness_score}


@dataclass(frozen=True)
class ScenarioPath:
    path_id: str
    decision_node: str
    confidence: float
    monetary_exposure: Decimal
    expected_benefit: Decimal
    reversible_state_hash: str

    @property
    def shadow_cost(self) -> Decimal:
        return (Decimal(str(self.confidence)) * self.monetary_exposure).quantize(Decimal('0.000001'))

    def as_dict(self) -> dict[str, Any]:
        return {'path_id': self.path_id, 'decision_node': self.decision_node, 'confidence': self.confidence, 'monetary_exposure': f'{self.monetary_exposure:.6f}', 'expected_benefit': f'{self.expected_benefit:.6f}', 'shadow_cost': f'{self.shadow_cost:.6f}', 'reversible_state_hash': self.reversible_state_hash}


class ScenarioSimulator:
    @staticmethod
    def make_path(path_id: str, decision_node: str, confidence: float, exposure: Decimal, expected_benefit: Decimal) -> ScenarioPath:
        if not 0 <= confidence <= 1 or exposure < 0 or expected_benefit < 0:
            raise ValueError('scenario values are outside their allowed ranges')
        raw = f'{path_id}:{decision_node}:{confidence}:{exposure}:{expected_benefit}'
        return ScenarioPath(path_id, decision_node, confidence, exposure, expected_benefit, hashlib.sha256(raw.encode()).hexdigest())

    @staticmethod
    def select(paths: list[ScenarioPath]) -> ScenarioPath | None:
        if not paths:
            return None
        # Prefer expected value after shadow exposure, with confidence as a tie-breaker.
        return max(paths, key=lambda path: (path.expected_benefit - path.shadow_cost, path.confidence))


def canonical_approval_payload(proposal_id: str, wallet_id: str, amount: Decimal, actor: str) -> bytes:
    body = {'actor': actor, 'amount': f'{amount:.6f}', 'proposal_id': proposal_id, 'wallet_id': wallet_id}
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def verify_ed25519_signature(public_key_b64: str, signature_b64: str, payload: bytes) -> bool:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature, payload)
        return True
    except (ValueError, InvalidSignature):
        return False
