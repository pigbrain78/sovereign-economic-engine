from __future__ import annotations

import json
from pathlib import Path

import app.sovereign_tool_registry as registry_mod
import ralph5_master as toolsmith_mod
from app.sovereign_tool_registry import SovereignToolRegistry
from ralph5_master import ToolsmithRunRequest
from ralph5_sandbox_runner import SandboxResult


def test_tool_registry_loads_fix_modules_and_executes(tmp_path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "fix_alpha.py").write_text(
        "def fix_alpha(name):\n"
        "    \"\"\"Greets.\"\"\"\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )

    registry = SovereignToolRegistry(str(tools_dir))
    schemas = registry.get_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "fix_alpha"
    assert registry.execute("fix_alpha", {"name": "world"}) == "hello world"


def test_toolsmith_promotes_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(toolsmith_mod, "TOOLS_DIR", tmp_path / "sovereign_tools")
    monkeypatch.setattr(toolsmith_mod, "LOGS_DIR", tmp_path / "sovereign_logs")
    monkeypatch.setattr(toolsmith_mod, "PROMOTION_LOG", (tmp_path / "sovereign_logs" / "promotion_ledger.jsonl"))
    monkeypatch.setattr(toolsmith_mod, "FAILURE_LOG", (tmp_path / "sovereign_logs" / "failed_experiments.jsonl"))
    monkeypatch.setattr(toolsmith_mod, "CONSTRAINTS_DB", (tmp_path / "sovereign_memory" / "system_constraints.json"))
    toolsmith_mod.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    toolsmith_mod.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    toolsmith_mod.CONSTRAINTS_DB.parent.mkdir(parents=True, exist_ok=True)
    toolsmith_mod.CONSTRAINTS_DB.write_text(json.dumps({"global_constraints": []}), encoding="utf-8")

    monkeypatch.setattr(
        toolsmith_mod,
        "_llm_candidate",
        lambda _req: ("def fix(v):\n    return v\n", "import tool\n\ndef test_fix():\n    assert tool.fix(7)==7\n"),
    )
    monkeypatch.setattr(
        toolsmith_mod,
        "execute_in_sandbox",
        lambda _req: SandboxResult(passed=True, stdout="ok", stderr="", exit_code=0, runtime_ms=1, failure_type="none"),
    )

    request = ToolsmithRunRequest(
        friction_id="FRIC-1",
        friction_point="x",
        root_cause="y",
        system_fix="z",
    )
    result = toolsmith_mod.orchestrate_toolsmith(request)
    assert result.promoted is True
    assert result.attempts == 1
    assert toolsmith_mod.PROMOTION_LOG.exists()
    assert len(list(toolsmith_mod.TOOLS_DIR.glob("fix_*.py"))) == 1


def test_toolsmith_archives_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(toolsmith_mod, "TOOLS_DIR", tmp_path / "sovereign_tools")
    monkeypatch.setattr(toolsmith_mod, "LOGS_DIR", tmp_path / "sovereign_logs")
    monkeypatch.setattr(toolsmith_mod, "PROMOTION_LOG", (tmp_path / "sovereign_logs" / "promotion_ledger.jsonl"))
    monkeypatch.setattr(toolsmith_mod, "FAILURE_LOG", (tmp_path / "sovereign_logs" / "failed_experiments.jsonl"))
    monkeypatch.setattr(toolsmith_mod, "CONSTRAINTS_DB", (tmp_path / "sovereign_memory" / "system_constraints.json"))
    toolsmith_mod.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    toolsmith_mod.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    toolsmith_mod.CONSTRAINTS_DB.parent.mkdir(parents=True, exist_ok=True)
    toolsmith_mod.CONSTRAINTS_DB.write_text(json.dumps({"global_constraints": []}), encoding="utf-8")

    monkeypatch.setattr(
        toolsmith_mod,
        "_llm_candidate",
        lambda _req: ("def fix(v):\n    return v + 1\n", "import tool\n\ndef test_fix():\n    assert tool.fix(7)==7\n"),
    )
    monkeypatch.setattr(
        toolsmith_mod,
        "execute_in_sandbox",
        lambda _req: SandboxResult(passed=False, stdout="", stderr="fail", exit_code=1, runtime_ms=1, failure_type="test_failure"),
    )

    request = ToolsmithRunRequest(
        friction_id="FRIC-2",
        friction_point="x",
        root_cause="y",
        system_fix="z",
        max_repair_attempts=2,
    )
    result = toolsmith_mod.orchestrate_toolsmith(request)
    assert result.promoted is False
    assert result.attempts == 2
    assert result.failure_reason == "MAX_REPAIR_ATTEMPTS_EXCEEDED"
    assert toolsmith_mod.FAILURE_LOG.exists()
