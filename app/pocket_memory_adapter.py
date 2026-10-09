from __future__ import annotations

from pathlib import Path
from typing import Any

from app.pocket_memory.api import MemoryAPI as PocketMemoryAPI
from app.pocket_memory.core import MemoryBrain
from app.pocket_memory.models import MEMORY_TYPES


class PocketMemoryAdapter:
    """Compatibility facade: Pocket OS memory-brain is authoritative internally."""

    def __init__(self, db_path: str | Path = 'sovereign-memory.db') -> None:
        self.brain = MemoryBrain(str(db_path), allow_auto_commit=False, require_council_for_irreversible=True)
        self.api = PocketMemoryAPI(brain=self.brain)

    @staticmethod
    def _type(memory_type: str | None) -> str:
        candidate = (memory_type or 'OBSERVATION').upper()
        return candidate if candidate in MEMORY_TYPES or candidate.startswith('X_') else 'OBSERVATION'

    def remember(self, content: str, *, memory_type: str | None = None, source_id: str | None = None, actor: str | None = None) -> dict[str, Any]:
        requested_type = memory_type or 'observation'
        result = self.api.remember(content, memory_type=self._type(memory_type), source_id=source_id, actor=actor)
        if result.get('requires_review'):
            return {**result, 'content': content, 'memory_type': requested_type}
        if not result.get('committed'):
            memory_id = result.get('memory_id') or result.get('duplicate_of')
            existing = self.brain.get(memory_id) if memory_id else None
            return {**(existing or {}), 'memory_id': memory_id, 'content': (existing or {}).get('content', content), 'memory_type': requested_type, 'deduplicated': True}
        memory = self.brain.get(result['memory_id']) or {}
        return {**memory, **result, 'memory_type': requested_type, 'deduplicated': False, 'retracted': False}

    def get(self, memory_id: str) -> dict[str, Any] | None:
        return self.brain.get(memory_id)

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        results = self.api.search(query, top_k=max(top_k, 50))
        return [result for result in results if float(result.get('relevance_score', 0)) > 0][:top_k]

    def retract(self, memory_id: str, reason: str = '', actor: str = 'system') -> dict[str, Any]:
        return self.api.retract(memory_id, reason, actor)

    def explain(self, memory_id: str) -> dict[str, Any]:
        return self.api.explain(memory_id)

    def verify_ledger(self) -> bool:
        return bool(self.api.integrity().get('valid', False))

    def health(self) -> dict[str, Any]:
        snapshot = self.api.health()
        return {
            'status': 'ok' if snapshot.get('health') == 'ok' else 'degraded',
            'active_memories': snapshot.get('totals', {}).get('memories', 0),
            'ledger_verified': snapshot.get('ledger', {}).get('integrity', {}).get('ok', False),
            'pocket_memory': snapshot,
        }

    def context(self, query: str, top_k: int = 10) -> dict[str, Any]:
        return self.api.context(query, top_k=top_k)

    def contradictions(self) -> list[dict[str, Any]]:
        return self.api.contradictions()
