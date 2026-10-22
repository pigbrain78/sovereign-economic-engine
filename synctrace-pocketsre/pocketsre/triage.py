"""Alert triage: rank open alerts so the on-call phone shows what matters first."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SEVERITY_WEIGHT = {"critical": 100, "high": 60, "medium": 30, "low": 10}


@dataclass(frozen=True)
class Alert:
    alert_id: str
    service: str
    severity: str
    summary: str
    minutes_open: int
    user_facing: bool = False


def score(alert: Alert) -> int:
    value = SEVERITY_WEIGHT.get(alert.severity, 0)
    if alert.user_facing:
        value += 25
    # Age adds up to 30 points so long-open alerts still surface.
    value += min(30, max(0, alert.minutes_open) // 10)
    return value


def rank(alerts: list[Alert]) -> list[dict[str, Any]]:
    ordered = sorted(alerts, key=lambda a: (-score(a), a.alert_id))
    return [{"alert_id": a.alert_id, "service": a.service, "score": score(a), "summary": a.summary}
            for a in ordered]
