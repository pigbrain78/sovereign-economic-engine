"""Governed Skill Forge contracts adapted from the skill-forge artifact.

The source repository is a React control surface with seeded examples, not a
backend authority. This module keeps the useful domain contracts while making
validation evidence explicit and fail closed: a skill cannot become approved
from metadata alone, and approval never authorizes execution or purchase.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


SKILL_STATUSES = {"draft", "testing", "approved", "mutated", "archived", "rejected"}
RISK_LEVELS = {"low", "medium", "high", "critical"}


@dataclass(frozen=True)
class SkillValidationDecision:
    status: str
    passed: bool
    reason: str
    compliance_score: float
    functional_score: float
    security_score: float
    governance_score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "reason": self.reason,
            "compliance_score": self.compliance_score,
            "functional_score": self.functional_score,
            "security_score": self.security_score,
            "governance_score": self.governance_score,
        }


def canonical_skill_payload(genome: dict[str, Any]) -> str:
    """Return a strict, deterministic JSON representation for skill identity."""
    return json.dumps(genome, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def skill_hash(genome: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_skill_payload(genome).encode("utf-8")).hexdigest()


def validate_genome(genome: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = ("skill_id", "version", "name", "purpose", "domain", "author", "permissions", "governance")
    for field in required:
        if not genome.get(field):
            errors.append(f"MISSING_GENOME_FIELD:{field}")
    permissions = genome.get("permissions")
    if not isinstance(permissions, dict):
        errors.append("INVALID_PERMISSIONS")
    else:
        for field in ("read", "write", "network", "filesystem", "financial", "exec_code"):
            aliases = {field, field.replace("_", ""), "execCode" if field == "exec_code" else field}
            if not any(alias in permissions for alias in aliases):
                errors.append(f"MISSING_PERMISSION:{field}")
    governance = genome.get("governance")
    if not isinstance(governance, dict):
        errors.append("INVALID_GOVERNANCE")
    else:
        if governance.get("risk_level") not in RISK_LEVELS:
            errors.append("INVALID_RISK_LEVEL")
        if not isinstance(governance.get("requires_approval"), bool):
            errors.append("MISSING_APPROVAL_POLICY")
    return errors


def evaluate_validation(
    genome: dict[str, Any],
    report: dict[str, Any],
) -> SkillValidationDecision:
    """Evaluate supplied evidence without treating claims as verified authority."""
    genome_errors = validate_genome(genome)
    functional = float(report.get("functional_score", 0))
    security = float(report.get("security_score", 0))
    governance = float(report.get("governance_score", 0))
    compliance = float(report.get("compliance_score", governance))
    security_issues = report.get("security_issues", [])
    report_passed = report.get("passed") is True
    sail_approved = genome.get("governance", {}).get("sail_approved") is True
    if genome_errors:
        return SkillValidationDecision("rejected", False, genome_errors[0], compliance, functional, security, governance)
    if not report_passed:
        return SkillValidationDecision("testing", False, "VALIDATION_REPORT_NOT_PASSED", compliance, functional, security, governance)
    if security_issues:
        return SkillValidationDecision("rejected", False, "SECURITY_ISSUES_PRESENT", compliance, functional, security, governance)
    if min(functional, security, governance, compliance) < 90:
        return SkillValidationDecision("testing", False, "SCORES_BELOW_APPROVAL_THRESHOLD", compliance, functional, security, governance)
    if not sail_approved:
        return SkillValidationDecision("testing", False, "SAIL_APPROVAL_UNVERIFIED", compliance, functional, security, governance)
    return SkillValidationDecision("approved", True, "VALIDATION_EVIDENCE_ACCEPTED", compliance, functional, security, governance)


def normalize_permissions(value: dict[str, Any]) -> dict[str, bool]:
    aliases = {"exec_code": "execCode", "execCode": "execCode"}
    normalized: dict[str, bool] = {}
    for field in ("read", "write", "network", "filesystem", "financial", "execCode"):
        source = next((key for key, target in aliases.items() if target == field and key in value), field)
        normalized[field] = bool(value.get(source, value.get(field, False)))
    return normalized
