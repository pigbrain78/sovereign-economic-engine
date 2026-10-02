from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class SandboxRequest(BaseModel):
    tool_code: str
    test_code: str
    timeout_seconds: int = Field(default=5, gt=0)
    memory_limit_mb: int = Field(default=256, gt=0)
    network_allowed: bool = False


class SandboxResult(BaseModel):
    passed: bool
    runtime_ms: int
    stdout: str = ""
    stderr: str = ""
    failure_type: Optional[str] = None
    exit_code: Optional[int] = None


def _memory_preexec(memory_limit_mb: int):
    def _apply_limit() -> None:
        import resource

        limit_bytes = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    return _apply_limit


def execute_in_sandbox(request: SandboxRequest) -> SandboxResult:
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ralph5_sandbox_") as workspace:
        workspace_path = Path(workspace)
        (workspace_path / "tool.py").write_text(request.tool_code, encoding="utf-8")
        (workspace_path / "test_tool.py").write_text(request.test_code, encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONPATH"] = workspace
        if not request.network_allowed:
            env["NO_PROXY"] = "*"

        try:
            completed = subprocess.run(
                ["python", "-m", "pytest", "-q", "test_tool.py"],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
                preexec_fn=_memory_preexec(request.memory_limit_mb),
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                passed=False,
                runtime_ms=int((time.perf_counter() - start) * 1000),
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                failure_type="TIMEOUT",
                exit_code=-1,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            return SandboxResult(
                passed=False,
                runtime_ms=int((time.perf_counter() - start) * 1000),
                stderr=str(exc),
                failure_type="SANDBOX_ERROR",
                exit_code=-1,
            )

        passed = completed.returncode == 0
        return SandboxResult(
            passed=passed,
            runtime_ms=int((time.perf_counter() - start) * 1000),
            stdout=completed.stdout,
            stderr=completed.stderr,
            failure_type=None if passed else "TEST_FAILURE",
            exit_code=completed.returncode,
        )
