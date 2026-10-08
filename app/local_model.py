from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.local_executor import LocalCommandExecutor, LocalExecutorUnavailable


class LocalModelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalModelResult:
    model_id: str
    runtime: str
    status: str
    output: str
    stderr: str
    duration_seconds: float
    input_bytes: int
    output_bytes: int
    token_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            'model_id': self.model_id,
            'runtime': self.runtime,
            'status': self.status,
            'output': self.output,
            'stderr': self.stderr,
            'duration_seconds': self.duration_seconds,
            'input_bytes': self.input_bytes,
            'output_bytes': self.output_bytes,
            'token_count': self.token_count,
        }


class LocalModelAdapter(Protocol):
    model_id: str
    runtime: str

    def status(self) -> dict[str, Any]: ...

    def execute(self, prompt: str, timeout_seconds: float) -> LocalModelResult: ...


class CommandModelAdapter:
    model_id = 'local-command'
    runtime = 'command'

    def __init__(self, executor: LocalCommandExecutor | None = None):
        self.executor = executor or LocalCommandExecutor()

    def status(self) -> dict[str, Any]:
        return {'model_id': self.model_id, 'runtime': self.runtime, **self.executor.status()}

    def execute(self, prompt: str, timeout_seconds: float) -> LocalModelResult:
        executor = LocalCommandExecutor(command=self.executor.command, timeout_seconds=timeout_seconds, driver=self.executor.driver)
        result = executor.execute(prompt)
        return LocalModelResult(self.model_id, self.runtime, result.status, result.output, result.stderr, result.duration_seconds, result.input_bytes, result.output_bytes, result.token_count)


class OllamaModelAdapter:
    runtime = 'ollama'

    def __init__(self, model_id: str, base_url: str = 'http://127.0.0.1:11434'):
        self.model_id = model_id
        self.base_url = base_url.rstrip('/')

    def status(self) -> dict[str, Any]:
        request = urllib.request.Request(f'{self.base_url}/api/tags', method='GET')
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode('utf-8'))
            models = [item.get('name') for item in payload.get('models', []) if isinstance(item, dict)]
            return {'model_id': self.model_id, 'runtime': self.runtime, 'configured': self.model_id in models, 'reachable': True, 'models': models, 'base_url': self.base_url, 'note': 'Local Ollama endpoint; no cloud provider call.'}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return {'model_id': self.model_id, 'runtime': self.runtime, 'configured': False, 'reachable': False, 'models': [], 'base_url': self.base_url, 'error': str(exc)}

    def execute(self, prompt: str, timeout_seconds: float) -> LocalModelResult:
        payload = json.dumps({'model': self.model_id, 'prompt': prompt, 'stream': False}).encode('utf-8')
        request = urllib.request.Request(f'{self.base_url}/api/generate', data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = json.loads(response.read().decode('utf-8'))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise LocalModelUnavailable(f'ollama execution failed: {exc}') from exc
        output = str(body.get('response', ''))
        return LocalModelResult(self.model_id, self.runtime, 'SUCCESS', output, '', Decimal(str(body.get('total_duration', 0) / 1_000_000_000)).__float__(), len(prompt.encode()), len(output.encode()), int(body.get('eval_count', len(output.split()))))


def configured_local_models() -> list[LocalModelAdapter]:
    adapters: list[LocalModelAdapter] = []
    command = CommandModelAdapter()
    if command.executor.command:
        adapters.append(command)
    ollama_model = os.environ.get('LOCAL_OLLAMA_MODEL')
    if ollama_model:
        adapters.append(OllamaModelAdapter(ollama_model, os.environ.get('LOCAL_OLLAMA_BASE_URL', 'http://127.0.0.1:11434')))
    return adapters


def local_model_status() -> dict[str, Any]:
    adapters = configured_local_models()
    return {'runtime': 'local', 'cloud_calls': False, 'models': [adapter.status() for adapter in adapters], 'configured': bool(adapters), 'note': 'Only explicitly configured local runtimes are listed; no fallback to a cloud provider.'}


def select_local_model(model_id: str | None = None) -> LocalModelAdapter:
    adapters = configured_local_models()
    if model_id:
        for adapter in adapters:
            if adapter.model_id == model_id:
                return adapter
        raise LocalModelUnavailable(f'local model {model_id!r} is not configured')
    if adapters:
        return adapters[0]
    raise LocalModelUnavailable('no local model configured; set LOCAL_EXECUTOR_COMMAND_JSON or LOCAL_OLLAMA_MODEL')
