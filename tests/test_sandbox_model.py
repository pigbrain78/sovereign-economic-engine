import json

import pytest

from app.local_model import LocalModelUnavailable
from app.sandbox_model import SandboxPolicy, SandboxedCommandModelAdapter
from app.substrate import ContainerDriver


def test_container_driver_builds_fail_closed_security_flags():
    args = ContainerDriver(engine_binary='podman').build_command_args('local-image:latest', ['python3', '-c', 'print(1)'])
    assert args[:4] == ['podman', 'run', '--rm', '--user']
    assert '--read-only' in args
    assert '--network' in args and args[args.index('--network') + 1] == 'none'
    assert '--cap-drop' in args and 'ALL' in args
    assert '--pids-limit' in args
    assert '--tmpfs' in args


def test_sandbox_fails_closed_without_engine():
    adapter = SandboxedCommandModelAdapter(SandboxPolicy(image='local-image:latest', command=['python3', '-c', 'print(1)'], engine_binary='definitely-missing-container-engine'))
    assert adapter.status()['configured'] is False
    with pytest.raises(LocalModelUnavailable):
        adapter.execute('hello', 2)


def test_sandbox_rejects_output_over_limit(monkeypatch):
    class FakeDriver:
        isolation_level = 'sandbox'
        def execute(self, command, timeout_s, input_text=None):
            from app.substrate import SandboxExecutionResult
            return SandboxExecutionResult(0, 'x' * 100, '', 1, False)

    adapter = SandboxedCommandModelAdapter(SandboxPolicy(image='local-image:latest', command=['python3'], engine_binary='python3', max_output_bytes=10), driver=FakeDriver())
    monkeypatch.setattr('shutil.which', lambda _: '/usr/bin/python3')
    with pytest.raises(LocalModelUnavailable, match='output exceeded'):
        adapter.execute('hello', 2)
