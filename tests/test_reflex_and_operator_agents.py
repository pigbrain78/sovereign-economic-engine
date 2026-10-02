from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.predictive_reflex_genome as prg_mod
from app.operator_worker_agent import MCPPermission, NodeTrustRegistry, OperatorWorkerAgent, SharedMCPContextGrant
from app.self_healing_reflex_agent import SelfHealingReflexAgent


def test_predictive_reflex_detects_drift_and_tightens_clamp(monkeypatch):
    genome = prg_mod.PredictiveReflexGenome()

    monkeypatch.setattr(prg_mod, "_ks_p_value", lambda _a, _b: 0.01)
    result = genome.evaluate_telemetry_drift([0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0])
    assert result["drift_detected"] is True
    assert genome.decay_lambda == 0.12
    assert genome.current_hardware_clamp == 5
    assert genome.get_hardware_clamp_kwargs()["mem_limit"] == "128m"


def test_predictive_reflex_restores_baseline_posture(monkeypatch):
    genome = prg_mod.PredictiveReflexGenome()
    genome.current_hardware_clamp = 5
    genome.decay_lambda = 0.12

    monkeypatch.setattr(prg_mod, "_ks_p_value", lambda _a, _b: 0.99)
    result = genome.evaluate_telemetry_drift([1, 1, 1, 1, 1], [1, 1, 1, 1, 1])
    assert result["drift_detected"] is False
    assert genome.decay_lambda == 0.05
    assert genome.current_hardware_clamp == 19
    assert genome.get_hardware_clamp_kwargs()["nano_cpus"] == 190_000_000


def test_self_healing_reflex_reacts_to_repeated_failures(monkeypatch):
    agent = SelfHealingReflexAgent()
    signals = {"dampened": 0, "rebooted": 0}

    monkeypatch.setattr(agent, "_trigger_state_dampening", lambda: signals.__setitem__("dampened", signals["dampened"] + 1))
    monkeypatch.setattr(agent, "_trigger_container_reboot", lambda: signals.__setitem__("rebooted", signals["rebooted"] + 1))
    monkeypatch.setattr(agent, "software_watchdog_ping", lambda: True)

    for _ in range(5):
        agent.log_task_execution(success=False)

    assert signals["dampened"] >= 1
    assert signals["rebooted"] == 0


def test_self_healing_reflex_reboots_on_watchdog_failure(monkeypatch):
    agent = SelfHealingReflexAgent()
    signals = {"rebooted": 0}
    monkeypatch.setattr(agent, "_trigger_container_reboot", lambda: signals.__setitem__("rebooted", signals["rebooted"] + 1))
    monkeypatch.setattr(agent, "software_watchdog_ping", lambda: False)

    agent.log_task_execution(success=True)
    assert signals["rebooted"] == 1


def test_operator_worker_enforces_trust_expiry_and_scope():
    registry = NodeTrustRegistry()
    agent = OperatorWorkerAgent(registry)
    valid_grant = SharedMCPContextGrant(
        host_node_id="FIRM_A_AGENT_01",
        guest_node_id="FIRM_A_AGENT_02",
        granted_scopes=[MCPPermission.EXECUTE_TOOL, MCPPermission.READ_STATE],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        cryptographic_signature="sig",
    )
    assert agent.mcp_proxy_gatekeeper(valid_grant, "tool.exec") is True

    low_trust = valid_grant.model_copy(update={"guest_node_id": "FIRM_C_AGENT_01"})
    assert agent.mcp_proxy_gatekeeper(low_trust, "tool.exec") is False

    expired = valid_grant.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    assert agent.mcp_proxy_gatekeeper(expired, "tool.exec") is False

    missing_scope = valid_grant.model_copy(update={"granted_scopes": [MCPPermission.READ_STATE]})
    assert agent.mcp_proxy_gatekeeper(missing_scope, "tool.exec") is False
