from __future__ import annotations

import io
import logging
import os
import tempfile
import time

from pydantic import BaseModel

from app.text import SandboxRequest as LocalSandboxRequest
from app.text import execute_in_sandbox as execute_in_local_sandbox

logger = logging.getLogger("ralph5_fortress")

try:  # Optional dependency
    import docker  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    docker = None  # type: ignore

try:  # pragma: no cover - runtime dependent
    client = docker.from_env() if docker is not None else None
except Exception:
    logger.error("CRITICAL: Docker daemon not running.")
    client = None

IMAGE_NAME = "ralph5-sandbox-base:latest"


class SandboxRequest(BaseModel):
    tool_code: str
    test_code: str
    timeout_seconds: int = 5
    memory_limit_mb: int = 256
    network_allowed: bool = False


class SandboxResult(BaseModel):
    passed: bool
    stdout: str
    stderr: str
    exit_code: int
    runtime_ms: int
    failure_type: str


def ensure_sandbox_image() -> None:
    if client is None or docker is None:
        return
    try:
        client.images.get(IMAGE_NAME)
    except docker.errors.ImageNotFound:
        dockerfile = (
            b"FROM python:3.11-slim\n"
            b"RUN pip install pytest\n"
            b"WORKDIR /sandbox\n"
            b"RUN useradd -m sandboxuser\n"
            b"USER sandboxuser\n"
        )
        client.images.build(fileobj=io.BytesIO(dockerfile), rm=True, tag=IMAGE_NAME)


def _fallback_local_runner(request: SandboxRequest) -> SandboxResult:
    result = execute_in_local_sandbox(
        LocalSandboxRequest(
            tool_code=request.tool_code,
            test_code=request.test_code,
            timeout_seconds=request.timeout_seconds,
            memory_limit_mb=request.memory_limit_mb,
            network_allowed=request.network_allowed,
        )
    )
    return SandboxResult(
        passed=result.passed,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code or (-1 if not result.passed else 0),
        runtime_ms=result.runtime_ms,
        failure_type=(result.failure_type or "none").lower(),
    )


def execute_in_sandbox(request: SandboxRequest) -> SandboxResult:
    if client is None or docker is None:
        return _fallback_local_runner(request)

    ensure_sandbox_image()
    start_time = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ralph5_fortress_") as temp_dir:
        with open(os.path.join(temp_dir, "tool.py"), "w", encoding="utf-8") as handle:
            handle.write(request.tool_code)
        with open(os.path.join(temp_dir, "test_tool.py"), "w", encoding="utf-8") as handle:
            handle.write("import sys, os\nsys.path.append(os.path.dirname(__file__))\n" + request.test_code)

        container = None
        try:
            container = client.containers.run(
                image=IMAGE_NAME,
                command=["python", "-m", "pytest", "test_tool.py", "-q", "--tb=short"],
                volumes={temp_dir: {"bind": "/sandbox", "mode": "ro"}},
                working_dir="/sandbox",
                detach=True,
                network_mode="bridge" if request.network_allowed else "none",
                mem_limit=f"{request.memory_limit_mb}m",
                pids_limit=50,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
            )

            timed_out = False
            while container.status in ("created", "running"):
                if time.perf_counter() - start_time > request.timeout_seconds:
                    timed_out = True
                    container.kill()
                    break
                time.sleep(0.1)
                container.reload()

            runtime_ms = int((time.perf_counter() - start_time) * 1000)
            if timed_out:
                return SandboxResult(
                    passed=False,
                    stdout="",
                    stderr="Timeout limit exceeded.",
                    exit_code=-1,
                    runtime_ms=runtime_ms,
                    failure_type="timeout",
                )

            result = container.wait()
            raw_logs = container.logs().decode("utf-8", errors="replace")
            passed = result["StatusCode"] == 0
            return SandboxResult(
                passed=passed,
                stdout=raw_logs if passed else "",
                stderr=raw_logs if not passed else "",
                exit_code=int(result["StatusCode"]),
                runtime_ms=runtime_ms,
                failure_type="none" if passed else "test_failure",
            )
        finally:
            if container:
                container.remove(force=True)
