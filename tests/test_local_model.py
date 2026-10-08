import json
import os

import pytest

from app.local_model import LocalModelUnavailable, local_model_status, select_local_model


def test_command_runtime_is_listed_without_cloud_fallback(monkeypatch):
    monkeypatch.setenv('LOCAL_EXECUTOR_COMMAND_JSON', json.dumps(['python3', '-c', 'print("ok")']))
    monkeypatch.delenv('LOCAL_OLLAMA_MODEL', raising=False)
    status = local_model_status()
    assert status['cloud_calls'] is False
    assert status['configured'] is True
    assert status['models'][0]['model_id'] == 'local-command'
    assert select_local_model().model_id == 'local-command'


def test_unknown_local_model_fails_closed(monkeypatch):
    monkeypatch.setenv('LOCAL_EXECUTOR_COMMAND_JSON', json.dumps(['python3', '-c', 'print("ok")']))
    with pytest.raises(LocalModelUnavailable):
        select_local_model('missing-local-model')


def test_ollama_is_optional_and_never_replaces_command(monkeypatch):
    monkeypatch.setenv('LOCAL_EXECUTOR_COMMAND_JSON', json.dumps(['python3', '-c', 'print("ok")']))
    monkeypatch.setenv('LOCAL_OLLAMA_MODEL', 'llama3.2:3b')
    assert select_local_model().model_id == 'local-command'
    status = local_model_status()
    assert {item['model_id'] for item in status['models']} == {'local-command', 'llama3.2:3b'}
