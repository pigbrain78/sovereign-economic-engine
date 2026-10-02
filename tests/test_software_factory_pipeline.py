from __future__ import annotations

import json

from app.software_factory_pipeline import FactoryPipelineRequest, SoftwareFactoryPipeline


def test_factory_rejects_ast_violation(tmp_path):
    ledger = tmp_path / "factory_ledger.jsonl"
    pipeline = SoftwareFactoryPipeline(str(ledger))
    request = FactoryPipelineRequest(
        tool_code="import os\n\ndef run():\n    return 1\n",
        test_code="import tool\n\ndef test_run():\n    assert tool.run() == 1\n",
        target_file="financials/tax.py",
    )

    result = pipeline.execute_pipeline(request)
    assert result.status == "REJECTED_AST"
    assert "blocked_import:os" in result.ast_report["violations"]
    assert result.ledger_hash is None


def test_factory_rejects_failed_sandbox(tmp_path):
    ledger = tmp_path / "factory_ledger.jsonl"
    pipeline = SoftwareFactoryPipeline(str(ledger))
    request = FactoryPipelineRequest(
        tool_code="def compute_tax(amount):\n    return amount * 0.10\n",
        test_code="import tool\n\ndef test_tax():\n    assert tool.compute_tax(100) == 15.0\n",
        target_file="financials/tax.py",
    )

    result = pipeline.execute_pipeline(request)
    assert result.status == "REJECTED_SANDBOX"
    assert result.sandbox_report is not None
    assert result.sandbox_report["passed"] is False
    assert result.ledger_hash is None


def test_factory_promotes_and_anchors_ledger(tmp_path):
    ledger = tmp_path / "factory_ledger.jsonl"
    pipeline = SoftwareFactoryPipeline(str(ledger))
    request = FactoryPipelineRequest(
        tool_code="def compute_tax(amount):\n    return amount * 0.15\n",
        test_code="import tool\n\ndef test_tax():\n    assert tool.compute_tax(100) == 15.0\n",
        target_file="financials/tax.py",
    )

    result = pipeline.execute_pipeline(request)
    assert result.status == "PROMOTED"
    assert result.ledger_hash is not None
    assert len(result.patch_hash) == 64

    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    genesis = json.loads(lines[0])
    promoted = json.loads(lines[1])
    assert genesis["event"] == "FACTORY_GENESIS"
    assert promoted["previous_hash"] == genesis["block_hash"]
