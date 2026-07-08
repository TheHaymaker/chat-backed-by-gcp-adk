"""Phase 3 tests: forms, handoff, cancel/reschedule — the interactive flows."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.registry.config import load_services
from agent.registry.registry import ToolRegistry
from agent.registry.mocks.crm import CrmError, InteractionTokenRequired
from gateway.main import build_state, create_app
from gateway.runner import MemorySink
from gateway.validation import EnvelopeValidator

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://localhost:3000"}


@pytest.fixture()
def registry():
    return ToolRegistry(services=load_services(ROOT / "services.yaml"), base_dir=ROOT)


@pytest.fixture()
def sink():
    return MemorySink()


@pytest.fixture()
def client(sink):
    state = build_state(services_path=ROOT / "services.yaml", sink=sink,
                        allowed_origins={"http://localhost:3000"})
    return TestClient(create_app(state))


def _session(client):
    r = client.post("/v1/session", json={"tenant": "acme"}, headers=ORIGIN)
    return {"Authorization": f"Bearer {r.json()['session_token']}", **ORIGIN}


def _envs(resp):
    return [json.loads(l[6:]) for l in resp.text.splitlines()
            if l.startswith("data: ") and l != "data: [DONE]"]


def _book(client, headers):
    r = client.post("/v1/chat", json={"message": "book a demo"}, headers=headers)
    sched = _envs(r)[0]["ui_blocks"][0]
    slot = sched["props"]["slots"][0]
    r = client.post("/v1/interact", json={
        "action": "slot_selected", "block_id": sched["id"],
        "payload": {"slot_id": slot["slot_id"]}}, headers=headers)
    return _envs(r)[0]["ui_blocks"][0]["props"]


# -- CRM adapter: token rule --------------------------------------------------

def test_capture_lead_requires_token(registry):
    crm = registry.adapter("sales-crm")
    with pytest.raises(InteractionTokenRequired):
        crm.capture_lead({"email": "a@b.co"})
    with pytest.raises(CrmError, match="email"):
        crm.capture_lead({"email": "not-an-email"}, "itx_x")
    lead = crm.capture_lead({"name": "Ada", "email": "ada@acme.example"}, "itx_x")
    assert lead["lead_ref"].startswith("ld_")
    # idempotent on identical payloads
    assert crm.capture_lead({"name": "Ada", "email": "ada@acme.example"}, "itx_x") == lead


def test_capture_lead_is_write_tool(registry):
    specs = {t.name: t for t in registry.tool_specs()}
    assert specs["sales-crm.capture_lead"].write is True


# -- schemas ----------------------------------------------------------------------

def test_form_and_handoff_schemas():
    v = EnvelopeValidator(contracts_dir=ROOT / "contracts")
    enabled = ["form", "handoff", "quick_replies"]
    clean, report = v.validate({
        "message": "m",
        "ui_blocks": [
            {"type": "form", "id": "blk_1", "props": {"form_id": "lead_capture",
                "fields": [{"name": "email", "label": "Email", "type": "email"}]}},
            {"type": "form", "id": "blk_2", "props": {"form_id": "bad",
                "fields": [{"name": "x", "label": "X", "type": "password"}]}},   # bad type
            {"type": "handoff", "id": "blk_3", "props": {"channels": [
                {"kind": "email", "label": "Email us", "value": "hi@a.example"}]}},
            {"type": "handoff", "id": "blk_4", "props": {"channels": []}},        # empty
            {"type": "quick_replies", "id": "blk_5", "props": {"options": [
                {"label": "Cancel it", "value": "cancel",
                 "action": "cancel_booking", "payload": {"booking_ref": "bk_1"}}]}},
        ],
        "events": [],
    }, enabled_blocks=enabled)
    assert [b["id"] for b in clean["ui_blocks"]] == ["blk_1", "blk_3", "blk_5"]
    assert len(report.dropped_blocks) == 2


# -- contact flow: form + handoff, then token-gated lead capture ---------------------

def test_contact_flow_form_submit(client, sink):
    headers = _session(client)
    r = client.post("/v1/chat", json={"message": "I want to contact support"},
                    headers=headers)
    env = _envs(r)[0]
    types = [b["type"] for b in env["ui_blocks"]]
    assert types == ["form", "handoff"]
    form = env["ui_blocks"][0]

    r = client.post("/v1/interact", json={
        "action": "form_submitted", "block_id": form["id"],
        "payload": {"form_id": "lead_capture",
                    "values": {"name": "Ada", "email": "ada@acme.example",
                               "message": "pricing question"}}}, headers=headers)
    env2 = _envs(r)[0]
    assert "reach out" in env2["message"]
    lead_evt = next(e for e in sink.events if e["name"] == "lead_captured")
    assert lead_evt["props"]["lead_ref"].startswith("ld_")
    assert lead_evt["tenant_id"] == "acme"


# -- cancel flow -----------------------------------------------------------------

def test_cancel_flow(client, sink):
    headers = _session(client)
    booking = _book(client, headers)

    r = client.post("/v1/chat", json={"message": "cancel my booking"}, headers=headers)
    env = _envs(r)[0]
    qr = env["ui_blocks"][0]
    assert qr["type"] == "quick_replies"
    opt = qr["props"]["options"][0]
    assert opt["action"] == "cancel_booking"
    assert opt["payload"]["booking_ref"] == booking["booking_ref"]

    r = client.post("/v1/interact", json={
        "action": "cancel_booking", "block_id": qr["id"],
        "payload": opt["payload"]}, headers=headers)
    env2 = _envs(r)[0]
    assert "cancelled" in env2["message"]
    assert any(e["name"] == "booking_cancelled" for e in sink.events)


def test_cancel_without_booking(client):
    r = client.post("/v1/chat", json={"message": "cancel my booking"},
                    headers=_session(client))
    assert "don't see an active booking" in _envs(r)[0]["message"]


# -- reschedule flow ---------------------------------------------------------------

def test_reschedule_flow(client, sink):
    headers = _session(client)
    first = _book(client, headers)

    r = client.post("/v1/chat", json={"message": "reschedule my demo"}, headers=headers)
    sched = _envs(r)[0]["ui_blocks"][0]
    assert sched["type"] == "scheduler"
    new_slot = sched["props"]["slots"][1]           # pick a different time

    r = client.post("/v1/interact", json={
        "action": "slot_selected", "block_id": sched["id"],
        "payload": {"slot_id": new_slot["slot_id"]}}, headers=headers)
    env = _envs(r)[0]
    assert "Rescheduled" in env["message"]
    conf = env["ui_blocks"][0]["props"]
    assert conf["start"] == new_slot["start"]
    assert conf["booking_ref"] != first["booking_ref"]

    resched = next(e for e in sink.events if e["name"] == "booking_rescheduled")
    assert resched["props"]["from"] == first["booking_ref"]
    assert resched["props"]["to"] == conf["booking_ref"]


def test_reschedule_releases_old_booking(client):
    headers = _session(client)
    first = _book(client, headers)
    client.post("/v1/chat", json={"message": "reschedule"}, headers=headers)
    r = client.post("/v1/chat", json={"message": "reschedule"}, headers=headers)
    sched = _envs(r)[0]["ui_blocks"][0]
    client.post("/v1/interact", json={
        "action": "slot_selected", "block_id": sched["id"],
        "payload": {"slot_id": sched["props"]["slots"][2]["slot_id"]}}, headers=headers)
    # old ref cancelled: cancelling it again should fail inside the adapter,
    # so a fresh cancel intent should offer only the NEW booking
    r = client.post("/v1/chat", json={"message": "cancel my booking"}, headers=headers)
    opt = _envs(r)[0]["ui_blocks"][0]["props"]["options"][0]
    assert opt["payload"]["booking_ref"] != first["booking_ref"]
