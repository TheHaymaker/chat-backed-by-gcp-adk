"""Gateway tests: the full mock-mode vertical slice, in-process."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.main import build_state, create_app
from gateway.runner import MemorySink
from gateway.security import TokenError, TokenService
from gateway.validation import EnvelopeValidator

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://localhost:3000"}


class ScriptedRunner:
    """Runner that replays canned raw envelopes, for validation tests."""
    def __init__(self, envelopes):
        self.envelopes = envelopes

    async def run_turn(self, session_id, user_message, context):
        for e in self.envelopes:
            yield e


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
    assert r.status_code == 200
    tok = r.json()["session_token"]
    return {"Authorization": f"Bearer {tok}", **ORIGIN}


def _sse_envelopes(resp) -> list[dict]:
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


# -- auth & origin ------------------------------------------------------------

def test_bad_origin_rejected(client):
    r = client.post("/v1/session", json={"tenant": "acme"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_chat_requires_session(client):
    r = client.post("/v1/chat", json={"message": "hi"}, headers=ORIGIN)
    assert r.status_code == 401


def test_config_reports_enabled_blocks(client):
    r = client.get("/v1/config", headers=_session(client))
    blocks = r.json()["enabled_blocks"]
    assert blocks[0] == "text"
    assert {"scheduler", "confirmation", "quick_replies"} <= set(blocks)


# -- chat flow with the mock runner ------------------------------------------------

def test_plain_chat_turn(client):
    r = client.post("/v1/chat", json={"message": "hello"}, headers=_session(client))
    envs = _sse_envelopes(r)
    assert len(envs) == 1
    assert envs[0]["message"].startswith("You said")
    assert envs[0]["ui_blocks"][0]["type"] == "quick_replies"


def test_booking_flow_end_to_end(client, sink):
    headers = _session(client)
    # 1. ask to book -> scheduler block with real mock-adapter slots
    r = client.post("/v1/chat", json={"message": "I want to book a demo"}, headers=headers)
    sched = _sse_envelopes(r)[0]["ui_blocks"][0]
    assert sched["type"] == "scheduler"
    slot = sched["props"]["slots"][0]

    # 2. user taps a slot -> /interact mints itx_ token, agent confirms
    r = client.post("/v1/interact", json={
        "action": "slot_selected", "block_id": sched["id"],
        "payload": {"slot_id": slot["slot_id"]},
    }, headers=headers)
    conf = _sse_envelopes(r)[0]
    assert conf["ui_blocks"][0]["type"] == "confirmation"
    assert conf["ui_blocks"][0]["props"]["start"] == slot["start"]

    # 3. booking_completed event landed in the sink, fully tagged
    names = [e["name"] for e in sink.events]
    assert "scheduler_offered" in names and "booking_completed" in names
    booked = next(e for e in sink.events if e["name"] == "booking_completed")
    assert booked["tenant_id"] == "acme" and booked["session_id"].startswith("s_")


# -- validation enforcement ---------------------------------------------------------

def _client_with_runner(envelopes, sink):
    state = build_state(services_path=ROOT / "services.yaml", sink=sink,
                        runner=ScriptedRunner(envelopes),
                        allowed_origins={"http://localhost:3000"})
    return TestClient(create_app(state))


def test_invalid_blocks_dropped_text_survives(sink):
    client = _client_with_runner([{
        "message": "Here you go",
        "ui_blocks": [
            {"type": "scheduler", "id": "blk_1", "props": {"service_id": "x"}},   # missing slots
            {"type": "hologram", "id": "blk_2", "props": {}},                      # unknown type
            {"type": "text", "id": "not-a-valid-id", "props": {"markdown": "x"}},  # bad id
            {"type": "text", "id": "blk_ok", "props": {"markdown": "kept"}},       # valid
        ],
        "events": [{"name": "ok_event"}, {"name": "BAD NAME"}],
    }], sink)
    envs = _sse_envelopes(client.post("/v1/chat", json={"message": "hi"},
                                      headers=_session(client)))
    env = envs[0]
    assert env["message"] == "Here you go"
    assert [b["id"] for b in env["ui_blocks"]] == ["blk_ok"]
    sanitized = next(e for e in sink.events if e["name"] == "envelope_sanitized")
    reasons = {d["reason"].split(":")[0] for d in sanitized["props"]["dropped"]}
    assert {"props_invalid", "unknown_type", "bad_block_id"} <= reasons


def test_malformed_envelope_degrades_to_text(sink):
    client = _client_with_runner([["not", "an", "envelope"]], sink)
    envs = _sse_envelopes(client.post("/v1/chat", json={"message": "hi"},
                                      headers=_session(client)))
    assert envs[0]["ui_blocks"] == []
    assert "Sorry" in envs[0]["message"]


def test_raw_html_in_markdown_passes_schema_but_is_bounded():
    # XSS defense is renderer-side (sanitized markdown) + schema length caps here.
    v = EnvelopeValidator(contracts_dir=ROOT / "contracts")
    clean, report = v.validate({
        "message": "m",
        "ui_blocks": [{"type": "text", "id": "blk_1",
                        "props": {"markdown": "<script>x</script>" * 1000}}],
        "events": [],
    }, enabled_blocks=["text"])
    assert clean["ui_blocks"] == []            # exceeds maxLength -> dropped
    assert report.dropped_blocks[0]["reason"].startswith("props_invalid")


# -- interaction tokens -------------------------------------------------------------

def test_interaction_token_binding():
    ts = TokenService(secret=b"k")
    tok = ts.mint_interaction("s_1", "slot_selected", {"slot_id": "a:b:c"})
    ts.verify_interaction(tok, "s_1", "slot_selected", {"slot_id": "a:b:c"})
    with pytest.raises(TokenError, match="session mismatch"):
        ts.verify_interaction(tok, "s_2", "slot_selected", {"slot_id": "a:b:c"})
    with pytest.raises(TokenError, match="payload mismatch"):
        ts.verify_interaction(tok, "s_1", "slot_selected", {"slot_id": "OTHER"})
    with pytest.raises(TokenError):
        ts.verify_interaction("itx_forged.token", "s_1", "slot_selected", {})


def test_interaction_token_expiry():
    ts = TokenService(secret=b"k", interaction_ttl=-1)
    tok = ts.mint_interaction("s_1", "slot_selected", {})
    with pytest.raises(TokenError, match="expired"):
        ts.verify_interaction(tok, "s_1", "slot_selected", {})


def test_session_token_tamper_rejected():
    ts = TokenService(secret=b"k")
    tok = ts.mint_session("acme", "s_1")
    with pytest.raises(TokenError):
        ts.verify_session(tok[:-2] + "zz")
    with pytest.raises(TokenError):
        TokenService(secret=b"other").verify_session(tok)


# -- events ingest -----------------------------------------------------------------

def test_widget_events_tagged_and_sunk(client, sink):
    headers = _session(client)
    r = client.post("/v1/events", json={"events": [
        {"name": "widget_opened", "props": {}},
        {"name": "ui_block_rendered", "props": {"type": "scheduler"}},
    ]}, headers=headers)
    assert r.json()["accepted"] == 2
    assert all(e["source"] == "widget" and e["tenant_id"] == "acme" for e in sink.events)
