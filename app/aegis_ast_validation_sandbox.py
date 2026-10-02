from __future__ import annotations

import ast
import hashlib
from pydantic import BaseModel, Field


class AegisValidationResult(BaseModel):
    ok: bool
    patch_hash: str
    violations: list[str] = Field(default_factory=list)
    target_file: str

    def to_dict(self) -> dict:
        return self.model_dump()


class AegisASTValidationSandbox:
    """Lightweight structural validator for generated Python patch code."""

    _BLOCKED_IMPORTS = {
        "ctypes",
        "multiprocessing",
        "os",
        "pathlib",
        "resource",
        "shutil",
        "signal",
        "socket",
        "subprocess",
        "sys",
    }
    _BLOCKED_CALLS = {"__import__", "compile", "eval", "exec", "open"}

    @staticmethod
    def _patch_hash(patch_code: str) -> str:
        return hashlib.sha256(patch_code.encode("utf-8")).hexdigest()

    @classmethod
    def validate_patch_code(cls, patch_code: str, target_file: str) -> AegisValidationResult:
        violations: list[str] = []
        patch_hash = cls._patch_hash(patch_code)

        try:
            tree = ast.parse(patch_code)
        except SyntaxError as exc:
            return AegisValidationResult(
                ok=False,
                patch_hash=patch_hash,
                violations=[f"syntax_error:{exc.msg}"],
                target_file=target_file,
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in cls._BLOCKED_IMPORTS:
                        violations.append(f"blocked_import:{alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    if root in cls._BLOCKED_IMPORTS:
                        violations.append(f"blocked_import:{node.module}")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in cls._BLOCKED_CALLS:
                    violations.append(f"blocked_call:{node.func.id}")
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"system", "popen", "run"} and isinstance(node.func.value, ast.Name):
                        if node.func.value.id in {"os", "subprocess"}:
                            violations.append(f"blocked_call:{node.func.value.id}.{node.func.attr}")

        return AegisValidationResult(
            ok=not violations,
            patch_hash=patch_hash,
            violations=sorted(set(violations)),
            target_file=target_file,
        )
