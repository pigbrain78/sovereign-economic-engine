from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxRejected(ValueError):
    """Raised when a candidate is structurally unsafe or fails closed verification."""


@dataclass(frozen=True)
class VerificationReceipt:
    candidate_hash: str
    files: int
    bytes_written: int
    checks: tuple[dict[str, Any], ...]
    passed: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            'candidate_hash': self.candidate_hash,
            'files': self.files,
            'bytes_written': self.bytes_written,
            'checks': list(self.checks),
            'passed': self.passed,
            'reason': self.reason,
        }


@dataclass(frozen=True)
class PromotionReceipt:
    candidate_hash: str
    active_path: str
    previous_path: str | None
    promoted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            'candidate_hash': self.candidate_hash,
            'active_path': self.active_path,
            'previous_path': self.previous_path,
            'promoted': self.promoted,
        }


class SandboxPromotion:
    """Small fail-closed promotion reference layer.

    Candidate files are written below a private root after strict relative-path
    validation. Verification is deterministic and intentionally conservative:
    Python files must compile, JSON files must parse, and empty candidates are
    rejected. Activation uses an atomic pointer replacement; rollback restores
    the previous pointer and never deletes it before the replacement succeeds.
    """

    MAX_FILES = 128
    MAX_FILE_BYTES = 2_000_000
    MAX_TOTAL_BYTES = 10_000_000
    ALLOWED_SUFFIXES = {'.py', '.json', '.toml', '.yaml', '.yml', '.md', '.txt'}

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.environ.get('SOVEREIGN_SANDBOX_ROOT', Path.cwd() / '.sandbox')).resolve()
        self.candidates = self.root / 'candidates'
        self.active_pointer = self.root / 'active.json'
        self.root.mkdir(parents=True, exist_ok=True)
        self.candidates.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_path(name: str) -> Path:
        if not name or '\\' in name or name.startswith('/'):
            raise SandboxRejected('candidate path must be a non-empty relative POSIX path')
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.name in {'', '.', '..'}:
            raise SandboxRejected('candidate path traversal rejected')
        if len(path.parts) > 8 or path.suffix.lower() not in SandboxPromotion.ALLOWED_SUFFIXES:
            raise SandboxRejected('candidate path or file type is not allowed')
        return path

    def _candidate_hash(self, files: dict[str, str]) -> str:
        canonical = []
        for name in sorted(files):
            canonical.append({'path': name, 'content': files[name]})
        return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()).hexdigest()

    def stage(self, candidate_id: str, files: dict[str, str]) -> tuple[Path, VerificationReceipt]:
        if not candidate_id or len(candidate_id) > 96 or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.' for ch in candidate_id):
            raise SandboxRejected('candidate id is invalid')
        if not files or len(files) > self.MAX_FILES:
            raise SandboxRejected('candidate must contain between 1 and 128 files')
        total = 0
        normalized: dict[str, str] = {}
        for name, content in files.items():
            safe = self._safe_path(name)
            if not isinstance(content, str):
                raise SandboxRejected('candidate file content must be text')
            size = len(content.encode('utf-8'))
            if size > self.MAX_FILE_BYTES:
                raise SandboxRejected(f'candidate file exceeds {self.MAX_FILE_BYTES} bytes')
            total += size
            normalized[safe.as_posix()] = content
        if total > self.MAX_TOTAL_BYTES:
            raise SandboxRejected(f'candidate exceeds {self.MAX_TOTAL_BYTES} total bytes')

        candidate_hash = self._candidate_hash(normalized)
        destination = self.candidates / candidate_id
        if destination.exists():
            raise SandboxRejected('candidate id already exists')
        temporary = Path(tempfile.mkdtemp(prefix=f'.{candidate_id}-', dir=self.candidates))
        checks: list[dict[str, Any]] = []
        try:
            for name, content in normalized.items():
                target = temporary / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding='utf-8', newline='')
            for name, content in normalized.items():
                suffix = Path(name).suffix.lower()
                if suffix == '.py':
                    try:
                        compile(content, name, 'exec')
                    except SyntaxError as exc:
                        checks.append({'check': 'python_compile', 'path': name, 'passed': False, 'error': str(exc)})
                        continue
                    checks.append({'check': 'python_compile', 'path': name, 'passed': True})
                elif suffix == '.json':
                    try:
                        json.loads(content)
                    except json.JSONDecodeError as exc:
                        checks.append({'check': 'json_parse', 'path': name, 'passed': False, 'error': str(exc)})
                        continue
                    checks.append({'check': 'json_parse', 'path': name, 'passed': True})
                else:
                    checks.append({'check': 'file_policy', 'path': name, 'passed': True})
            passed = all(item['passed'] for item in checks)
            receipt = VerificationReceipt(candidate_hash, len(normalized), total, tuple(checks), passed, 'VERIFIED' if passed else 'VERIFICATION_FAILED')
            if not passed:
                shutil.rmtree(temporary, ignore_errors=True)
                return destination, receipt
            temporary.rename(destination)
            (destination / 'verification_receipt.json').write_text(json.dumps(receipt.as_dict(), sort_keys=True, indent=2), encoding='utf-8')
            return destination, receipt
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def promote(self, candidate_id: str, expected_hash: str) -> PromotionReceipt:
        candidate = self.candidates / candidate_id
        receipt_path = candidate / 'verification_receipt.json'
        if not candidate.is_dir() or not receipt_path.is_file():
            raise SandboxRejected('candidate is not staged')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        if not receipt.get('passed') or receipt.get('candidate_hash') != expected_hash:
            raise SandboxRejected('candidate verification receipt is missing, failed, or mismatched')
        previous_path = None
        if self.active_pointer.exists():
            previous = json.loads(self.active_pointer.read_text(encoding='utf-8'))
            previous_path = previous.get('candidate_path')
        payload = {'candidate_id': candidate_id, 'candidate_hash': expected_hash, 'candidate_path': str(candidate), 'previous_path': previous_path}
        temporary = self.active_pointer.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
        os.replace(temporary, self.active_pointer)
        return PromotionReceipt(expected_hash, str(candidate), previous_path, True)

    def rollback(self, expected_hash: str) -> PromotionReceipt:
        if not self.active_pointer.exists():
            raise SandboxRejected('no active candidate to roll back')
        active = json.loads(self.active_pointer.read_text(encoding='utf-8'))
        if active.get('candidate_hash') != expected_hash:
            raise SandboxRejected('active candidate hash does not match rollback request')
        previous_path = active.get('previous_path')
        if not previous_path:
            self.active_pointer.unlink()
            return PromotionReceipt(expected_hash, '', None, False)
        previous = Path(previous_path)
        if not previous.is_dir():
            raise SandboxRejected('previous candidate is unavailable; rollback held')
        payload = {'candidate_id': previous.name, 'candidate_hash': self._receipt_hash(previous), 'candidate_path': str(previous), 'previous_path': None}
        temporary = self.active_pointer.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
        os.replace(temporary, self.active_pointer)
        return PromotionReceipt(expected_hash, str(previous), str(previous), False)

    @staticmethod
    def _receipt_hash(candidate: Path) -> str:
        receipt = candidate / 'verification_receipt.json'
        return json.loads(receipt.read_text(encoding='utf-8'))['candidate_hash']

    def active(self) -> dict[str, Any] | None:
        if not self.active_pointer.exists():
            return None
        return json.loads(self.active_pointer.read_text(encoding='utf-8'))
