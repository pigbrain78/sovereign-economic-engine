from __future__ import annotations

import json

import pytest

import app.dr_quinn_post_mortem as drq
from app.dr_quinn_post_mortem import DrQuinnPostMortem


class _FakeResponse:
    class _Choice:
        class _Message:
            content = json.dumps({"analysis": "null check missing", "generalized_rule": "Always validate nullable inputs."})

        message = _Message()

    choices = [_Choice()]


class _FakeCompletions:
    @staticmethod
    def create(**_kwargs):
        return _FakeResponse()


class _FakeChat:
    completions = _FakeCompletions()


class _FakeClient:
    chat = _FakeChat()


def test_post_mortem_sweep_updates_constraints_and_archives(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    memory_dir = tmp_path / "memory"
    engine = DrQuinnPostMortem(str(logs_dir), str(memory_dir))
    engine.failed_log.write_text(
        json.dumps({"friction_id": "fx-1", "sandbox_history": [{"error": "TypeError"}]}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(drq, "llm_client", _FakeClient())

    processed = engine.run_post_mortem_sweep()
    assert processed == 1

    constraints = json.loads(engine.constraints_db.read_text(encoding="utf-8"))
    assert constraints["global_constraints"][0]["source"] == "fx-1"
    assert constraints["global_constraints"][0]["rule"] == "Always validate nullable inputs."
    assert engine.archive_log.read_text(encoding="utf-8").strip()
    assert engine.failed_log.read_text(encoding="utf-8") == ""


def test_post_mortem_sweep_noop_without_failed_entries(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    memory_dir = tmp_path / "memory"
    engine = DrQuinnPostMortem(str(logs_dir), str(memory_dir))
    monkeypatch.setattr(drq, "llm_client", _FakeClient())

    assert engine.run_post_mortem_sweep() == 0


def test_post_mortem_sweep_fails_without_llm(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    memory_dir = tmp_path / "memory"
    engine = DrQuinnPostMortem(str(logs_dir), str(memory_dir))
    engine.failed_log.write_text(json.dumps({"friction_id": "fx-1", "sandbox_history": []}) + "\n", encoding="utf-8")
    monkeypatch.setattr(drq, "llm_client", None)

    with pytest.raises(ValueError, match="LLM client required"):
        engine.run_post_mortem_sweep()
