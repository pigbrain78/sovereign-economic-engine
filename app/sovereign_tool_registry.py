from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("tool_registry")
TOOLS_DIR = "./sovereign_tools"


class SovereignToolRegistry:
    def __init__(self, tools_dir: str = TOOLS_DIR):
        self.tools_dir = Path(tools_dir)
        self.loaded_functions: dict[str, Any] = {}
        self.openai_schemas: list[dict[str, Any]] = []
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.refresh_tools()

    def refresh_tools(self) -> None:
        self.loaded_functions.clear()
        self.openai_schemas.clear()
        for filename in os.listdir(self.tools_dir):
            if filename.startswith("fix_") and filename.endswith(".py"):
                self._load_tool(self.tools_dir / filename, filename)

    def _load_tool(self, filepath: Path, filename: str) -> None:
        mod_name = filename[:-3]
        spec = importlib.util.spec_from_file_location(mod_name, str(filepath))
        if not spec or not spec.loader:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        func = getattr(module, mod_name, None)
        if not func:
            funcs = [obj for name, obj in inspect.getmembers(module, inspect.isfunction) if obj.__module__ == mod_name]
            if funcs:
                func = funcs[0]
        if not func:
            return

        schema = self._generate_schema(func)
        self.loaded_functions[schema["function"]["name"]] = func
        self.openai_schemas.append(schema)

    def _generate_schema(self, func: Any) -> dict[str, Any]:
        sig = inspect.signature(func)
        desc = (inspect.getdoc(func) or "Tool").split("Args:")[0].strip()
        params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for name, parameter in sig.parameters.items():
            params["properties"][name] = {"type": "string", "description": name}
            if parameter.default == inspect.Parameter.empty:
                params["required"].append(name)
        return {
            "type": "function",
            "function": {
                "name": func.__name__,
                "description": desc,
                "parameters": params,
            },
        }

    def get_schemas(self) -> list[dict[str, Any]]:
        return self.openai_schemas

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        try:
            return self.loaded_functions[name](**args)
        except Exception as exc:
            return str(exc)
