from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("dr_quinn")

try:  # Optional dependency
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore

try:  # pragma: no cover - environment dependent
    llm_client = OpenAI() if OpenAI is not None else None
except Exception:  # pragma: no cover - environment dependent
    llm_client = None


class DrQuinnPostMortem:
    def __init__(self, logs_dir: str = "./sovereign_logs", memory_dir: str = "./sovereign_memory"):
        self.logs_dir = Path(logs_dir)
        self.memory_dir = Path(memory_dir)
        self.failed_log = self.logs_dir / "failed_experiments.jsonl"
        self.archive_log = self.logs_dir / "archived_experiments.jsonl"
        self.constraints_db = self.memory_dir / "system_constraints.json"
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        if not self.constraints_db.exists():
            self.constraints_db.write_text(json.dumps({"global_constraints": []}, indent=2), encoding="utf-8")

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load_constraints(self) -> dict[str, Any]:
        try:
            return json.loads(self.constraints_db.read_text(encoding="utf-8"))
        except Exception:
            return {"global_constraints": []}

    def _save_constraints(self, constraints: dict[str, Any]) -> None:
        self.constraints_db.write_text(json.dumps(constraints, indent=2), encoding="utf-8")

    @staticmethod
    def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists() or path.stat().st_size == 0:
            return []
        entries: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    entries.append(json.loads(line))
        return entries

    def _analyze_failure(self, entry: dict[str, Any]) -> dict[str, Any]:
        if llm_client is None:
            raise ValueError("LLM client required for post-mortem sweep.")

        sys_msg = "Extract the fundamental code error. Output JSON with keys: 'analysis', 'generalized_rule'."
        usr_msg = f"Failed Fix: {entry['friction_id']}\nHistory: {json.dumps(entry.get('sandbox_history', []))}"
        res = llm_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}],
            response_format={"type": "json_object"},
        )
        return json.loads(res.choices[0].message.content)

    def run_post_mortem_sweep(self) -> int:
        failed_entries = self._parse_jsonl(self.failed_log)
        if not failed_entries:
            return 0

        logger.info("Dr. Quinn sweeping graveyard...")
        constraints = self._load_constraints()
        constraints.setdefault("global_constraints", [])

        for entry in failed_entries:
            analysis = self._analyze_failure(entry)
            constraints["global_constraints"].append(
                {
                    "source": entry["friction_id"],
                    "date": self._utcnow(),
                    "rule": analysis["generalized_rule"],
                }
            )
            logger.info("Learned Rule: %s", analysis["generalized_rule"])

        self._save_constraints(constraints)
        with self.archive_log.open("a", encoding="utf-8") as archive:
            for entry in failed_entries:
                archive.write(json.dumps(entry) + "\n")
        self.failed_log.write_text("", encoding="utf-8")
        return len(failed_entries)


def run_post_mortem_sweep(logs_dir: str = "./sovereign_logs", memory_dir: str = "./sovereign_memory") -> int:
    """Module-level entrypoint for automation loops."""
    return DrQuinnPostMortem(logs_dir=logs_dir, memory_dir=memory_dir).run_post_mortem_sweep()
