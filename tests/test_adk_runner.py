"""Tests for AdkRunner plumbing that runs without a live agent."""
import os

from gateway.adk_runner import parse_envelope
from gateway.main import _default_runner
from gateway.runner import MockAgentRunner


def test_parse_clean_envelope():
    env = parse_envelope('{"message": "hi", "ui_blocks": [], "events": []}')
    assert env["message"] == "hi"


def test_parse_fenced_envelope():
    env = parse_envelope('```json\n{"message": "hi"}\n```')
    assert env == {"message": "hi", "ui_blocks": [], "events": []}


def test_parse_envelope_with_surrounding_prose():
    env = parse_envelope('Sure! {"message": "hi", "ui_blocks": []} Hope that helps.')
    assert env["message"] == "hi" and env["events"] == []


def test_parse_garbage_degrades_to_text():
    env = parse_envelope("I am just prose, no JSON here.")
    assert env["ui_blocks"] == [] and "prose" in env["message"]
    # and the gateway validator would still pass this as a text-only envelope


def test_runner_selection_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("AGENT_RUNNER", raising=False)
    from agent.registry.config import load_services
    from agent.registry.registry import ToolRegistry
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    reg = ToolRegistry(services=load_services(root / "services.yaml"), base_dir=root)
    assert isinstance(_default_runner(reg), MockAgentRunner)


def test_runner_selection_adk(monkeypatch):
    monkeypatch.setenv("AGENT_RUNNER", "adk")
    monkeypatch.setenv("ADK_BASE_URL", "http://agent.internal:8080")
    from gateway.adk_runner import AdkRunner
    r = _default_runner(None)
    assert isinstance(r, AdkRunner)
    assert r.base_url == "http://agent.internal:8080"
