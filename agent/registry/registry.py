"""Tool Registry.

Turns a validated ServicesFile into:
  1. ToolSpec objects — framework-agnostic (name, description, schema, callable).
     A thin ADK binding wraps each callable as a FunctionTool (see agent/app).
  2. The "integrated services" prompt section for the root agent.

Mode routing: mock -> in-process adapters; live -> transport adapters (Phase 6;
raises NotImplementedError until then so a misconfigured deploy fails loudly).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Mode, ServiceConfig, ServicesFile
from .mocks.crm import MockCrmAdapter
from .mocks.custom import MockCustomAdapter
from .mocks.knowledge import MockFaqAdapter, MockKbAdapter, MockSearchAdapter
from .mocks.scheduling import MockSchedulingAdapter


@dataclass
class ToolSpec:
    name: str                      # namespaced: "{service_id}.{capability}"
    service_id: str
    capability: str
    description: str
    write: bool
    fn: Callable[..., dict]


@dataclass
class ToolRegistry:
    services: ServicesFile
    base_dir: Path
    _adapters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for svc in self.services.services:
            self._adapters[svc.id] = self._make_adapter(svc)

    def _make_adapter(self, svc: ServiceConfig) -> Any:
        if svc.mode == Mode.live:
            return self._make_live_adapter(svc)
        if svc.kind == "scheduling":
            return MockSchedulingAdapter(service=svc)
        if svc.kind == "faq":
            return MockFaqAdapter(service=svc, base_dir=self.base_dir)
        if svc.kind == "knowledge_base":
            return MockKbAdapter(service=svc, base_dir=self.base_dir)
        if svc.kind == "site_search":
            return MockSearchAdapter(service=svc, base_dir=self.base_dir)
        if svc.kind == "crm":
            return MockCrmAdapter(service=svc)
        if svc.kind == "custom":
            return MockCustomAdapter(service=svc, base_dir=self.base_dir)
        raise ValueError(f"no adapter for kind '{svc.kind}'")

    @staticmethod
    def _make_live_adapter(svc: ServiceConfig) -> Any:
        transport = svc.live.transport
        if transport == "webhook":
            from .live.webhook import WebhookAdapter
            return WebhookAdapter(service=svc)
        if transport == "mcp":
            from .live.mcp_openapi import McpAdapter
            return McpAdapter(service=svc)
        if transport == "openapi":
            from .live.mcp_openapi import OpenApiAdapter
            return OpenApiAdapter(service=svc)
        raise ValueError(f"unknown live transport '{transport}'")

    def adapter(self, service_id: str) -> Any:
        return self._adapters[service_id]

    def invoke(self, service_id: str, capability: str, **kwargs: Any) -> dict:
        """Call a capability through the same binding the model's tools use —
        works identically for mock and live adapters."""
        svc = next(s for s in self.services.services if s.id == service_id)
        adapter = self._adapters[service_id]
        bind = self._bind_custom if svc.kind == "custom" else self._bind_kind
        return bind(adapter, capability)(**kwargs)

    # -- tool generation -----------------------------------------------------
    def tool_specs(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for svc in self.services.services:
            adapter = self._adapters[svc.id]
            if svc.kind == "custom":
                for tool in svc.tools:
                    specs.append(ToolSpec(
                        name=f"{svc.id}.{tool.name}",
                        service_id=svc.id,
                        capability=tool.name,
                        description=tool.description or svc.description,
                        write=tool.write,
                        fn=self._bind_custom(adapter, tool.name),
                    ))
            else:
                for cap in svc.capabilities:
                    specs.append(ToolSpec(
                        name=f"{svc.id}.{cap}",
                        service_id=svc.id,
                        capability=cap,
                        description=f"[{svc.id}] {cap.replace('_', ' ')} — {svc.description.strip()}",
                        write=svc.is_write(cap),
                        fn=self._bind_kind(adapter, cap),
                    ))
        return specs

    @staticmethod
    def _bind_kind(adapter: Any, cap: str) -> Callable[..., dict]:
        if hasattr(adapter, "invoke"):          # live transports
            def _call(**kwargs: Any) -> dict:
                return adapter.invoke(cap, kwargs)
            _call.__name__ = cap
            return _call
        method = getattr(adapter, cap)          # mock adapters
        def _call(**kwargs: Any) -> dict:
            # kind adapters expose capability-named methods; map common args
            if cap == "get_availability":
                return method(kwargs["date"], kwargs.get("duration_minutes", 30))
            return method(**kwargs)
        _call.__name__ = cap
        return _call

    @staticmethod
    def _bind_custom(adapter: Any, tool_name: str) -> Callable[..., dict]:
        def _call(**kwargs: Any) -> dict:
            if hasattr(adapter, "invoke"):      # live transports
                return adapter.invoke(tool_name, kwargs)
            token = kwargs.pop("interaction_token", None)
            return adapter.call(tool_name, kwargs, interaction_token=token)
        _call.__name__ = tool_name
        return _call

    # -- prompt assembly -------------------------------------------------------
    def prompt_section(self) -> str:
        lines = ["## Integrated services", ""]
        for svc in self.services.services:
            hints = f" Prefer for: {', '.join(svc.routing_hints)}." if svc.routing_hints else ""
            tools = ", ".join(svc.tool_names())
            lines.append(f"- **{svc.id}** ({svc.kind}, {svc.mode.value}): {svc.description.strip()}{hints}")
            lines.append(f"  Tools: {tools}")
        lines += [
            "",
            "Rules: never invent services or tools not listed above. "
            "Write operations (holds, confirmations, cancellations) may only be "
            "called with an interaction_token produced by a user tap on a UI block — "
            "never fabricate one. If two services could apply, use routing hints or ask.",
        ]
        return "\n".join(lines)
