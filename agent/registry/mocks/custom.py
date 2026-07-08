"""Fixture-backed mock adapter for kind: custom services."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import ServiceConfig


class CustomToolError(Exception):
    pass


@dataclass
class MockCustomAdapter:
    service: ServiceConfig
    base_dir: Path

    def _fixtures(self) -> dict:
        if not self.service.mock.fixtures:
            return {}
        path = self.base_dir / self.service.mock.fixtures
        if not path.exists():
            raise CustomToolError(f"fixture file not found: {path}")
        return json.loads(path.read_text())

    def call(self, tool_name: str, args: dict, interaction_token: str | None = None) -> dict:
        tool = next((t for t in self.service.tools if t.name == tool_name), None)
        if tool is None:
            raise CustomToolError(f"unknown tool '{tool_name}' on service '{self.service.id}'")
        if tool.write and (not interaction_token or not interaction_token.startswith("itx_")):
            raise CustomToolError(f"tool '{tool_name}' is a write tool and requires interaction_token")
        fixtures = self._fixtures().get(tool_name, {})
        # Key fixtures by the first required input field's value; fall back to _default.
        required = tool.input_schema.get("required", [])
        key = str(args.get(required[0])) if required else "_default"
        result = fixtures.get(key, fixtures.get("_default"))
        if result is None:
            raise CustomToolError(f"no fixture for {tool_name}({key}) and no _default provided")
        return result
