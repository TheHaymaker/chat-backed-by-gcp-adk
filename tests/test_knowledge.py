"""Phase 2 tests: FAQ / knowledge base / site search, mock-first."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.registry.config import load_services
from agent.registry.registry import ToolRegistry
from gateway.main import build_state, create_app
from gateway.runner import MemorySink

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
    tok = r.json()["session_token"]
    return {"Authorization": f"Bearer {tok}", **ORIGIN}


def _envs(resp):
    return [json.loads(l[6:]) for l in resp.text.splitlines()
            if l.startswith("data: ") and l != "data: [DONE]"]


# -- adapters ---------------------------------------------------------------------

def test_faq_lookup_scores_and_ranks(registry):
    a = registry.adapter("help-center")
    matches = a.lookup("how much does the pro plan cost")["matches"]
    assert matches and matches[0]["question"].startswith("How much")
    assert matches[0]["score"] >= a.MIN_SCORE
    # deterministic
    assert a.lookup("how much does the pro plan cost") == a.lookup("how much does the pro plan cost")


def test_faq_below_floor_returns_empty(registry):
    assert registry.adapter("help-center").lookup("quantum entanglement")["matches"] == []


def test_kb_chunks_all_citable(registry):
    chunks = registry.adapter("docs-kb").retrieve("verify webhook signature header")["chunks"]
    assert chunks
    assert all(c["title"] and c["url"].startswith("https://") for c in chunks)
    assert chunks[0]["title"] == "Webhook signatures"


def test_kb_grounding_floor(registry):
    assert registry.adapter("docs-kb").retrieve("medieval falconry techniques")["chunks"] == []


def test_site_search_ranks_pages(registry):
    hits = registry.adapter("site-search").search("enterprise pricing plans")["results"]
    assert hits[0]["title"] == "Pricing"


def test_knowledge_tools_are_read_only(registry):
    specs = {t.name: t for t in registry.tool_specs()}
    for name in ("help-center.lookup", "docs-kb.retrieve", "site-search.search"):
        assert specs[name].write is False


# -- end-to-end through the gateway ---------------------------------------------------

def test_faq_flow(client, sink):
    r = client.post("/v1/chat", json={"message": "what is your refund policy?"},
                    headers=_session(client))
    block = _envs(r)[0]["ui_blocks"][0]
    assert block["type"] == "faq_card"
    assert "30 days" in block["props"]["answer_markdown"]
    assert any(e["name"] == "faq_answered" for e in sink.events)


def test_kb_flow_has_citations(client):
    r = client.post("/v1/chat", json={"message": "explain webhook signature verification"},
                    headers=_session(client))
    block = _envs(r)[0]["ui_blocks"][0]
    assert block["type"] == "kb_answer"
    cites = block["props"]["citations"]
    assert len(cites) >= 1 and all(c["url"].startswith("https://") for c in cites)


def test_kb_no_answer_says_so(client, sink):
    r = client.post("/v1/chat", json={"message": "how does medieval falconry work?"},
                    headers=_session(client))
    env = _envs(r)[0]
    assert "won't guess" in env["message"] or "couldn't find" in env["message"]
    assert all(b["type"] != "kb_answer" for b in env["ui_blocks"])
    assert any(e["name"] == "kb_no_answer" for e in sink.events)


def test_search_flow(client):
    r = client.post("/v1/chat", json={"message": "find the pricing page"},
                    headers=_session(client))
    block = _envs(r)[0]["ui_blocks"][0]
    assert block["type"] == "search_results"
    assert block["props"]["results"][0]["title"] == "Pricing"


def test_booking_still_routes_to_scheduler(client):
    r = client.post("/v1/chat", json={"message": "book a demo"}, headers=_session(client))
    assert _envs(r)[0]["ui_blocks"][0]["type"] == "scheduler"


# -- contract: new block schemas enforced by the gateway validator ------------------

def test_faq_card_schema_enforced(sink):
    from gateway.validation import EnvelopeValidator
    v = EnvelopeValidator(contracts_dir=ROOT / "contracts")
    enabled = ["text", "faq_card", "kb_answer"]
    clean, report = v.validate({
        "message": "m",
        "ui_blocks": [
            {"type": "faq_card", "id": "blk_1", "props": {"question": "q"}},          # missing answer
            {"type": "kb_answer", "id": "blk_2", "props": {"markdown": "x",
                                                            "citations": []}},          # empty citations
            {"type": "kb_answer", "id": "blk_3", "props": {"markdown": "x",
                "citations": [{"title": "t", "url": "https://a.example/d"}]}},          # valid
        ],
        "events": [],
    }, enabled_blocks=enabled)
    assert [b["id"] for b in clean["ui_blocks"]] == ["blk_3"]
    assert len(report.dropped_blocks) == 2
