from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Any

from app.local_model import LocalModelResult, LocalModelUnavailable
from app.substrate import ContainerDriver, ContainerLimits, SubprocessDriver


@dataclass(frozen=True)
class SandboxPolicy:
    image: str
    command: list[str]
    engine_binary: str = 'podman'
    timeout_seconds: float = 30.0
    max_output_bytes: int = 256_000
    memory_mb: int = 512
    pids_limit: int = 64
    network_disabled: bool = True


class SandboxedCommandModelAdapter:
    """Rootless container adapter; unavailable unless the engine and image are explicit."""

    runtime = 'rootless_container'

    def __init__(self, policy: SandboxPolicy, driver: SubprocessDriver | None = None):
        self.policy = policy
        self.model_id = f'sandbox:{policy.image}'
        self.container = ContainerDriver(ContainerLimits(memory_mb=policy.memory_mb, pids_limit=policy.pids_limit, network_disabled=policy.network_disabled), policy.engine_binary)
        self.driver = driver or SubprocessDriver()

    def status(self) -> dict[str, Any]:
        engine = shutil.which(self.policy.engine_binary)
        configured = bool(engine and self.policy.image and self.policy.command)
        return {
            'model_id': self.model_id,
            'runtime': self.runtime,
            'configured': configured,
            'engine': self.policy.engine_binary,
            'engine_path': engine,
            'image': self.policy.image,
            'network_disabled': self.policy.network_disabled,
            'memory_mb': self.policy.memory_mb,
            'pids_limit': self.policy.pids_limit,
            'max_output_bytes': self.policy.max_output_bytes,
            'note': 'Rootless container, read-only rootfs, dropped capabilities, no network, bounded tmpfs.' if configured else 'Sandbox unavailable until the engine and image are installed and explicitly configured.',
        }

    def execute(self, prompt: str, timeout_seconds: float) -> LocalModelResult:
        status = self.status()
        if not status['configured']:
            raise LocalModelUnavailable('sandbox unavailable: install the configured rootless container engine and provide an image')
        args = self.container.build_command_args(self.policy.image, self.policy.command)
        result = self.driver.execute(args, timeout_s=min(timeout_seconds, self.policy.timeout_seconds), input_text=prompt)
        output = result.stdout or ''
        if len(output.encode('utf-8')) > self.policy.max_output_bytes:
            raise LocalModelUnavailable('sandbox output exceeded max_output_bytes')
        state = 'TIMED_OUT' if result.timed_out else ('SUCCESS' if result.exit_code == 0 else 'FAILED')
        duration = result.duration_ms / 1000.0
        return LocalModelResult(self.model_id, self.runtime, state, output, result.stderr or '', duration, len(prompt.encode('utf-8')), len(output.encode('utf-8')), len(output.split()))


def configured_sandbox_model() -> SandboxedCommandModelAdapter | None:
    image = os.environ.get('LOCAL_SANDBOX_IMAGE')
    raw_command = os.environ.get('LOCAL_SANDBOX_COMMAND_JSON')
    if not image or not raw_command:
        return None
    try:
        command = json.loads(raw_command)
    except json.JSONDecodeError as exc:
        raise LocalModelUnavailable('LOCAL_SANDBOX_COMMAND_JSON is not valid JSON') from exc
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise LocalModelUnavailable('LOCAL_SANDBOX_COMMAND_JSON must be a non-empty string array')
    policy = SandboxPolicy(
        image=image,
        command=command,
        engine_binary=os.environ.get('LOCAL_SANDBOX_ENGINE', 'podman'),
        timeout_seconds=float(os.environ.get('LOCAL_SANDBOX_TIMEOUT_SECONDS', '30')),
        max_output_bytes=int(os.environ.get('LOCAL_SANDBOX_MAX_OUTPUT_BYTES', '256000')),
        memory_mb=int(os.environ.get('LOCAL_SANDBOX_MEMORY_MB', '512')),
        pids_limit=int(os.environ.get('LOCAL_SANDBOX_PIDS_LIMIT', '64')),
    )
    return SandboxedCommandModelAdapter(policy)
