"""Service Integration Config: models, loading, validation.

`services.yaml` is the single source of truth for what backend services the
agent is integrated with. This module parses and validates it. Consumers:
  - toolgen.py   -> generates namespaced tools per service
  - prompt.py    -> assembles the "integrated capabilities" prompt section
  - gateway      -> derives enabled UI blocks (capability negotiation)
"""
from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

SERVICE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")

# Canonical capabilities per kind; 'custom' declares tools inline instead.
KIND_CAPABILITIES: dict[str, set[str]] = {
    "scheduling": {
        "list_services",
        "get_availability",
        "hold_slot",
        "confirm_booking",
        "cancel_booking",
    },
    "faq": {"lookup"},
    "knowledge_base": {"retrieve"},
    "site_search": {"search"},
    "crm": {"capture_lead"},
}
# Write capabilities require a client interaction token (human-tap rule).
WRITE_CAPABILITIES: set[str] = {"hold_slot", "confirm_booking", "cancel_booking",
                                "capture_lead"}


class Mode(str, Enum):
    mock = "mock"
    live = "live"


class MockConfig(BaseModel):
    fixtures: Optional[str] = None
    latency_ms: int = Field(default=0, ge=0, le=10_000)
    failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    seed: Optional[int] = None  # override determinism seed if desired


class LiveConfig(BaseModel):
    transport: str = Field(pattern=r"^(webhook|mcp|openapi)$")
    base_url: Optional[str] = None
    url: Optional[str] = None
    spec_url: Optional[str] = None
    auth: Optional[str] = None  # e.g. secret-manager://key, oidc, hmac
    timeout_ms: int = Field(default=5000, ge=100, le=60_000)


class CustomTool(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    description: str = ""
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)
    write: bool = False


class ServiceConfig(BaseModel):
    id: str
    kind: str
    mode: Mode = Mode.mock
    description: str
    routing_hints: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    tools: list[CustomTool] = Field(default_factory=list)  # kind: custom only
    ui_blocks: list[str] = Field(default_factory=lambda: ["text"])
    mock: MockConfig = Field(default_factory=MockConfig)
    live: Optional[LiveConfig] = None

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not SERVICE_ID_RE.match(v):
            raise ValueError(f"service id '{v}' must match {SERVICE_ID_RE.pattern}")
        return v

    @model_validator(mode="after")
    def _kind_rules(self) -> "ServiceConfig":
        if self.kind == "custom":
            if not self.tools:
                raise ValueError(f"service '{self.id}': kind=custom requires inline `tools`")
            if self.capabilities:
                raise ValueError(f"service '{self.id}': use `tools`, not `capabilities`, for kind=custom")
        else:
            allowed = KIND_CAPABILITIES.get(self.kind)
            if allowed is None:
                raise ValueError(f"service '{self.id}': unknown kind '{self.kind}'")
            if not self.capabilities:
                raise ValueError(f"service '{self.id}': at least one capability required")
            unknown = set(self.capabilities) - allowed
            if unknown:
                raise ValueError(f"service '{self.id}': unknown capabilities {sorted(unknown)} for kind '{self.kind}'")
        if self.mode == Mode.live and self.live is None:
            raise ValueError(f"service '{self.id}': mode=live requires a `live` section")
        return self

    def tool_names(self) -> list[str]:
        """Fully namespaced tool names exposed to the model."""
        caps = [t.name for t in self.tools] if self.kind == "custom" else self.capabilities
        return [f"{self.id}.{c}" for c in caps]

    def is_write(self, capability: str) -> bool:
        if self.kind == "custom":
            for t in self.tools:
                if t.name == capability:
                    return t.write
            raise KeyError(capability)
        return capability in WRITE_CAPABILITIES


class ServicesFile(BaseModel):
    version: int = 1
    tenant: str
    services: list[ServiceConfig]

    @model_validator(mode="after")
    def _unique_ids(self) -> "ServicesFile":
        ids = [s.id for s in self.services]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate service ids: {sorted(dupes)}")
        return self

    def enabled_ui_blocks(self) -> list[str]:
        """Union of blocks across services — gateway sends this to the widget."""
        blocks: list[str] = ["text"]
        for s in self.services:
            for b in s.ui_blocks:
                if b not in blocks:
                    blocks.append(b)
        return blocks


def load_services(path: str | Path) -> ServicesFile:
    # Expand ${VAR} references so a live overlay can be parameterized per
    # environment (e.g. base_url: ${WEBHOOK_BASE_URL}/sales-scheduler). Unset
    # vars are left verbatim so validation still surfaces a clear config error.
    text = os.path.expandvars(Path(path).read_text())
    raw = yaml.safe_load(text)
    return ServicesFile.model_validate(raw)
