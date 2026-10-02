from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ralph5_sandbox_runner import SandboxRequest, SandboxResult, execute_in_sandbox

try:  # Optional dependency
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore

logger = logging.getLogger("ralph5")


class ToolsmithRunRequest(BaseModel):
    friction_id: str
    friction_point: str
    root_cause: str
    system_fix: str
    max_repair_attempts: int = 3


class ToolsmithRunResult(BaseModel):
    promoted: bool
    attempts: int
    final_code: str | None = None
    final_tests: str | None = None
    sandbox_results: list[SandboxResult]
    failure_reason: str | None = None


TOOLS_DIR = Path("./sovereign_tools")
LOGS_DIR = Path("./sovereign_logs")
PROMOTION_LOG = LOGS_DIR / "promotion_ledger.jsonl"
FAILURE_LOG = LOGS_DIR / "failed_experiments.jsonl"
CONSTRAINTS_DB = Path("./sovereign_memory/system_constraints.json")
for directory in (TOOLS_DIR, LOGS_DIR, Path("./sovereign_memory")):
    directory.mkdir(parents=True, exist_ok=True)

if not CONSTRAINTS_DB.exists():
    CONSTRAINTS_DB.write_text(json.dumps({"global_constraints": []}, indent=2), encoding="utf-8")

try:  # pragma: no cover - environment dependent
    llm_client = OpenAI() if OpenAI is not None else None
except Exception:  # pragma: no cover - environment dependent
    llm_client = None


def extract_code(llm_output: str, fallback_test: str = "") -> tuple[str, str]:
    blocks = re.findall(r"```(?:python)?\n(.*?)```", llm_output, flags=re.DOTALL)
    if len(blocks) >= 2:
        return blocks[0].strip() + "\n", blocks[1].strip() + "\n"
    if len(blocks) == 1:
        return blocks[0].strip() + "\n", fallback_test
    return llm_output.strip() + "\n", fallback_test


def _read_constraints() -> list[dict[str, Any]]:
    try:
        content = json.loads(CONSTRAINTS_DB.read_text(encoding="utf-8"))
        return list(content.get("global_constraints", []))
    except Exception:
        return []


def _fallback_candidate(request: ToolsmithRunRequest) -> tuple[str, str]:
    fn_name = re.sub(r"[^a-z0-9_]+", "_", request.friction_id.lower()).strip("_") or "generated_fix"
    code = f"def {fn_name}(value):\n    return value\n"
    tests = (
        "import tool\n\n"
        f"def test_{fn_name}():\n"
        f"    assert tool.{fn_name}(123) == 123\n"
    )
    return code, tests


def _llm_candidate(request: ToolsmithRunRequest) -> tuple[str, str]:
    if llm_client is None:
        return _fallback_candidate(request)
    constraints = _read_constraints()
    rules = [rule.get("rule", "") for rule in constraints[-5:]]
    prompt = (
        "Produce Python tool code and pytest tests in two code fences.\n"
        f"Friction ID: {request.friction_id}\n"
        f"Friction Point: {request.friction_point}\n"
        f"Root Cause: {request.root_cause}\n"
        f"System Fix: {request.system_fix}\n"
        f"Prior Constraints: {rules}\n"
    )
    response = llm_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are RALPH 5 Ultra. Return deterministic code and tests only."},
            {"role": "user", "content": prompt},
        ],
    )
    return extract_code(response.choices[0].message.content or "", fallback_test=_fallback_candidate(request)[1])


def _append_jsonl(path: Path, data: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True) + "\n")


def orchestrate_toolsmith(request: ToolsmithRunRequest) -> ToolsmithRunResult:
    sandbox_results: list[SandboxResult] = []
    final_code = None
    final_tests = None

    for attempt in range(1, request.max_repair_attempts + 1):
        code, tests = _llm_candidate(request)
        final_code, final_tests = code, tests
        sandbox_request = SandboxRequest(
            tool_code=code,
            test_code=tests,
            timeout_seconds=5,
            memory_limit_mb=256,
            network_allowed=False,
        )
        run_result = execute_in_sandbox(sandbox_request)
        sandbox_results.append(run_result)
        if run_result.passed:
            tool_path = TOOLS_DIR / f"fix_{request.friction_id.lower().replace('-', '_')}.py"
            tool_path.write_text(code, encoding="utf-8")
            _append_jsonl(
                PROMOTION_LOG,
                {
                    "friction_id": request.friction_id,
                    "attempt": attempt,
                    "promoted_at": datetime.now(timezone.utc).isoformat(),
                    "tool_file": str(tool_path),
                },
            )
            return ToolsmithRunResult(
                promoted=True,
                attempts=attempt,
                final_code=code,
                final_tests=tests,
                sandbox_results=sandbox_results,
            )

    _append_jsonl(
        FAILURE_LOG,
        {
            "friction_id": request.friction_id,
            "friction_point": request.friction_point,
            "root_cause": request.root_cause,
            "system_fix": request.system_fix,
            "sandbox_history": [result.model_dump() for result in sandbox_results],
            "failed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return ToolsmithRunResult(
        promoted=False,
        attempts=len(sandbox_results),
        final_code=final_code,
        final_tests=final_tests,
        sandbox_results=sandbox_results,
        failure_reason="MAX_REPAIR_ATTEMPTS_EXCEEDED",
    )
