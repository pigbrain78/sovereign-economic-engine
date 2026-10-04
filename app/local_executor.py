from __future__ import annotations

import json
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from app.substrate import SubprocessDriver


class LocalExecutorUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalExecutionResult:
    status: str
    output: str
    stderr: str
    duration_seconds: float
    input_bytes: int
    output_bytes: int
    token_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            'status': self.status,
            'output': self.output,
            'stderr': self.stderr,
            'duration_seconds': self.duration_seconds,
            'input_bytes': self.input_bytes,
            'output_bytes': self.output_bytes,
            'token_count': self.token_count,
        }


class LocalCommandExecutor:
    """Runs a user-configured local command; it is not a security sandbox."""

    def __init__(self, command: list[str] | None = None, timeout_seconds: float = 30.0, driver: SubprocessDriver | None = None):
        self.command = command or self._configured_command()
        self.timeout_seconds = timeout_seconds
        self.driver = driver or SubprocessDriver()

    @staticmethod
    def _configured_command() -> list[str] | None:
        raw = os.environ.get('LOCAL_EXECUTOR_COMMAND_JSON')
        if not raw:
            return None
        try:
            command = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LocalExecutorUnavailable('LOCAL_EXECUTOR_COMMAND_JSON is not valid JSON') from exc
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise LocalExecutorUnavailable('LOCAL_EXECUTOR_COMMAND_JSON must be a non-empty string array')
        return command

    def status(self) -> dict[str, Any]:
        return {
            'configured': bool(self.command),
            'runtime': 'local_command',
            'isolation': self.driver.isolation_level,
            'note': 'Local command execution requires an operator-supplied command and is not a security sandbox.' if self.command else 'No local executor configured; local execution fails closed.',
        }

    def execute(self, prompt: str) -> LocalExecutionResult:
        if not self.command:
            raise LocalExecutorUnavailable('no local executor configured; set LOCAL_EXECUTOR_COMMAND_JSON')
        encoded = prompt.encode('utf-8')
        started = perf_counter()
        result = self.driver.execute(self.command, timeout_s=self.timeout_seconds, input_text=prompt)
        duration = max(result.duration_ms / 1000.0, perf_counter() - started)
        output = result.stdout or ''
        status = 'TIMED_OUT' if result.timed_out else ('SUCCESS' if result.exit_code == 0 else 'FAILED')
        return LocalExecutionResult(status, output, result.stderr or '', duration, len(encoded), len(output.encode('utf-8')), len(output.split()))
