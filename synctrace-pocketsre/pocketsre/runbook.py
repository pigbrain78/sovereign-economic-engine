"""Runbook steps that only run with an explicit approval record.

A destructive step never runs from a bare request. The approval must name the
step, match the runbook digest, and carry a named human approver. Both the start
and the finish of each step are written to the ledger. Steps are plain callables
registered in code; no code is loaded from outside the app.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from core.ledger import Ledger


class RunbookRejected(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    step_id: str
    description: str
    action: Callable[[dict[str, Any]], dict[str, Any]]
    destructive: bool = False


class Runbook:
    def __init__(self, name: str, steps: list[Step], ledger: Ledger) -> None:
        self.name = name
        self.steps = {s.step_id: s for s in steps}
        self.ledger = ledger

    def digest(self) -> str:
        spec = [{"id": s.step_id, "desc": s.description, "destructive": s.destructive}
                for s in sorted(self.steps.values(), key=lambda s: s.step_id)]
        return hashlib.sha256(json.dumps({"name": self.name, "steps": spec},
                                         sort_keys=True).encode()).hexdigest()

    def run(self, step_id: str, context: dict[str, Any],
            approval: dict[str, Any] | None = None) -> dict[str, Any]:
        step = self.steps.get(step_id)
        if step is None:
            raise RunbookRejected(f"unknown step {step_id}")
        if step.destructive:
            if not approval:
                raise RunbookRejected("destructive step requires approval")
            if approval.get("step_id") != step_id or approval.get("runbook_digest") != self.digest():
                raise RunbookRejected("approval does not match this step and runbook")
            approver = approval.get("approver")
            if not approver or approver == "system":
                raise RunbookRejected("approval needs a named human approver")
        self.ledger.append("RUNBOOK_STEP_STARTED", {"step": step_id, "approval": approval})
        result = step.action(context)
        self.ledger.append("RUNBOOK_STEP_FINISHED", {"step": step_id, "result": result})
        return result
