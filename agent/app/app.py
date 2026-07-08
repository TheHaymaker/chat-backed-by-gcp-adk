"""ADK App wiring.

Binds the framework-agnostic ToolRegistry to Google ADK:
  registry ToolSpecs -> FunctionTools on the root LlmAgent
  registry.prompt_section() -> injected into the agent instruction
  Plugins attached per architecture: Model Armor + BigQuery Agent Analytics
  (enabled via env; safe no-ops locally so `adk web` runs with zero GCP setup).

Requires: pip install google-adk  (plus [bigquery-analytics] extra for telemetry).
This module import-guards ADK so the registry/tests run without it installed.
"""
from __future__ import annotations

import os
from pathlib import Path

from agent.registry.config import load_services
from agent.registry.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]

BASE_INSTRUCTION = """\
You are a website assistant embedded in a chat widget. You respond ONLY with the
JSON envelope: {"message": str, "ui_blocks": [...], "events": [...]}.

UI block policy:
- Plain answers -> a single `text` block (markdown, no raw HTML ever).
- Offering appointment times -> `scheduler` block with slots from get_availability,
  including the service_id the slots came from.
- After confirm_booking succeeds -> `confirmation` block.
- Suggest next steps with `quick_replies` (max 5).
- Never invent block types; never place executable content in any field.

{services_section}
"""


def build_registry() -> ToolRegistry:
    services_path = os.environ.get("SERVICES_CONFIG", str(ROOT / "services.yaml"))
    cfg = load_services(services_path)
    return ToolRegistry(services=cfg, base_dir=Path(services_path).parent)


def build_app():
    """Construct the ADK App. Import ADK lazily so non-agent tooling stays light."""
    from google.adk.agents import Agent
    from google.adk.apps import App
    from google.adk.tools import FunctionTool

    registry = build_registry()
    tools = []
    for spec in registry.tool_specs():
        fn = spec.fn
        fn.__doc__ = spec.description + (
            " REQUIRES interaction_token from a user tap." if spec.write else ""
        )
        tools.append(FunctionTool(fn))

    instruction = BASE_INSTRUCTION.format(services_section=registry.prompt_section())

    root_agent = Agent(
        name="web_chat_root",
        model=os.environ.get("MODEL", "gemini-flash-latest"),
        instruction=instruction,
        tools=tools,
    )

    plugins = []
    if os.environ.get("ENABLE_MODEL_ARMOR") == "1":
        from agent.plugins.model_armor import build_model_armor_plugin  # Phase 4 deepening
        plugins.append(build_model_armor_plugin())
    if os.environ.get("ENABLE_BQ_ANALYTICS") == "1":
        from google.adk.plugins.bigquery_agent_analytics_plugin import (
            BigQueryAgentAnalyticsPlugin,
        )
        plugins.append(BigQueryAgentAnalyticsPlugin(
            project_id=os.environ["GOOGLE_CLOUD_PROJECT"],
            dataset_id=os.environ.get("BQ_ANALYTICS_DATASET_ID", "agent_telemetry"),
        ))

    return App(name="web-chat-agent", root_agent=root_agent, plugins=plugins)
