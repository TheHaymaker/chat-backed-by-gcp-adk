"""Tests for the service config + tool registry + mock adapters."""
from pathlib import Path

import pytest

from agent.registry.config import ServicesFile, load_services
from agent.registry.mocks.scheduling import (
    HOLD_TTL_SECONDS,
    InteractionTokenRequired,
    MockSchedulingAdapter,
    SchedulingError,
)
from agent.registry.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def services() -> ServicesFile:
    return load_services(ROOT / "services.yaml")


@pytest.fixture()
def registry(services) -> ToolRegistry:
    return ToolRegistry(services=services, base_dir=ROOT)


# -- config validation -------------------------------------------------------

def test_loads_example_config(services):
    assert services.tenant == "acme"
    assert [s.id for s in services.services] == [
        "sales-scheduler", "support-calendar", "order-lookup",
        "help-center", "docs-kb", "site-search", "sales-crm"]


def test_duplicate_ids_rejected():
    raw = {"version": 1, "tenant": "t", "services": [
        {"id": "svc-a", "kind": "scheduling", "description": "x", "capabilities": ["get_availability"]},
        {"id": "svc-a", "kind": "scheduling", "description": "y", "capabilities": ["get_availability"]},
    ]}
    with pytest.raises(ValueError, match="duplicate"):
        ServicesFile.model_validate(raw)


def test_unknown_capability_rejected():
    raw = {"version": 1, "tenant": "t", "services": [
        {"id": "svc-a", "kind": "scheduling", "description": "x", "capabilities": ["teleport"]},
    ]}
    with pytest.raises(ValueError, match="unknown capabilities"):
        ServicesFile.model_validate(raw)


def test_custom_requires_inline_tools():
    raw = {"version": 1, "tenant": "t", "services": [
        {"id": "svc-a", "kind": "custom", "description": "x"},
    ]}
    with pytest.raises(ValueError, match="requires inline"):
        ServicesFile.model_validate(raw)


def test_live_mode_requires_live_section():
    raw = {"version": 1, "tenant": "t", "services": [
        {"id": "svc-a", "kind": "scheduling", "mode": "live", "description": "x",
         "capabilities": ["get_availability"]},
    ]}
    with pytest.raises(ValueError, match="requires a `live` section"):
        ServicesFile.model_validate(raw)


def test_ui_block_union(services):
    blocks = services.enabled_ui_blocks()
    assert blocks[0] == "text"
    assert "scheduler" in blocks and "confirmation" in blocks


# -- registry / tool generation ----------------------------------------------

def test_namespaced_tools(registry):
    names = {t.name for t in registry.tool_specs()}
    assert "sales-scheduler.get_availability" in names
    assert "support-calendar.get_availability" in names  # two schedulers coexist
    assert "order-lookup.get_order_status" in names


def test_write_flags(registry):
    by_name = {t.name: t for t in registry.tool_specs()}
    assert by_name["sales-scheduler.confirm_booking"].write is True
    assert by_name["sales-scheduler.get_availability"].write is False
    assert by_name["order-lookup.get_order_status"].write is False


def test_prompt_section_mentions_services_and_rules(registry):
    prompt = registry.prompt_section()
    for sid in ("sales-scheduler", "support-calendar", "order-lookup"):
        assert sid in prompt
    assert "interaction_token" in prompt
    assert "never invent" in prompt


def test_live_mode_builds_transport_adapter(services):
    services.services[0].mode = "live"
    services.services[0].live = {"transport": "webhook", "base_url": "https://x", "auth": "hmac-key"}
    svc = ServicesFile.model_validate(services.model_dump())
    from agent.registry.live.webhook import WebhookAdapter
    reg = ToolRegistry(services=svc, base_dir=ROOT)
    assert isinstance(reg.adapter("sales-scheduler"), WebhookAdapter)


# -- scheduling mock: determinism ---------------------------------------------

def test_availability_deterministic(registry):
    a = registry.adapter("sales-scheduler")
    s1 = a.get_availability("2026-07-09")
    s2 = a.get_availability("2026-07-09")
    assert s1 == s2
    assert len(s1["slots"]) >= 4
    assert s1["slots"][0]["slot_id"].startswith("sales-scheduler:2026-07-09:")


def test_availability_differs_by_service(registry):
    d = "2026-07-09"
    sales = registry.adapter("sales-scheduler").get_availability(d)
    support = registry.adapter("support-calendar").get_availability(d)
    assert sales["slots"] != support["slots"]


# -- scheduling mock: interaction-token rule ------------------------------------

def test_hold_requires_token(registry):
    a = registry.adapter("sales-scheduler")
    slot = a.get_availability("2026-07-09")["slots"][0]
    with pytest.raises(InteractionTokenRequired):
        a.hold_slot(slot["slot_id"])                       # no token
    with pytest.raises(InteractionTokenRequired):
        a.hold_slot(slot["slot_id"], "made-up-by-model")   # wrong shape
    hold = a.hold_slot(slot["slot_id"], "itx_abc123")
    assert hold["hold_id"].startswith("hold_")


def test_confirm_and_cancel_flow(registry):
    a = registry.adapter("sales-scheduler")
    slot = a.get_availability("2026-07-09")["slots"][0]
    hold = a.hold_slot(slot["slot_id"], "itx_abc123")
    booking = a.confirm_booking(hold["hold_id"], "itx_abc123")
    assert booking["booking_ref"].startswith("bk_")
    assert booking["start"] == slot["start"]
    assert a.cancel_booking(booking["booking_ref"], "itx_def456") == {"cancelled": True}


def test_confirm_without_token_blocked(registry):
    a = registry.adapter("sales-scheduler")
    slot = a.get_availability("2026-07-09")["slots"][0]
    hold = a.hold_slot(slot["slot_id"], "itx_abc123")
    with pytest.raises(InteractionTokenRequired):
        a.confirm_booking(hold["hold_id"])


# -- scheduling mock: hold expiry -----------------------------------------------

def test_expired_hold_rejected(services):
    clock = {"t": 1_000_000.0}
    svc = services.services[0]
    a = MockSchedulingAdapter(service=svc, now=lambda: clock["t"])
    slot = a.get_availability("2026-07-09")["slots"][0]
    hold = a.hold_slot(slot["slot_id"], "itx_abc123")
    clock["t"] += HOLD_TTL_SECONDS + 1
    with pytest.raises(SchedulingError, match="expired"):
        a.confirm_booking(hold["hold_id"], "itx_abc123")


# -- custom mock: fixtures ---------------------------------------------------------

def test_custom_fixture_lookup(registry):
    a = registry.adapter("order-lookup")
    shipped = a.call("get_order_status", {"order_ref": "A1001"})
    assert shipped["status"] == "shipped"
    unknown = a.call("get_order_status", {"order_ref": "NOPE"})
    assert unknown["status"] == "unknown"   # _default fallback


def test_custom_tool_via_registry_spec(registry):
    spec = next(t for t in registry.tool_specs() if t.name == "order-lookup.get_order_status")
    assert spec.fn(order_ref="A1002")["status"] == "processing"
