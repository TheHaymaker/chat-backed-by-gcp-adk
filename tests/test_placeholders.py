"""The placeholder backend serves every write/tool kind over the webhook wire.

Exercises placeholders/backend.py through the real WebhookAdapter (HMAC signing,
token rule, circuit breaker) via an in-process ASGI bridge — the same shape as
tests/contract/test_contract.py, but across scheduling + CRM + custom so the
multi-service backend is a permanent CI gate, not just a manual smoke script.
"""
import httpx
import pytest

from agent.registry.config import LiveConfig, Mode, ServicesFile, load_services
from agent.registry.live.base import InteractionTokenRequired
from agent.registry.live.webhook import WebhookAdapter
from placeholders.backend import create_app

SECRET = "placeholder-test-secret"
ROOT_SERVICES = "services.yaml"


def _asgi_transport(app) -> httpx.MockTransport:
    from starlette.testclient import TestClient
    tc = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        r = tc.request(request.method, request.url.path,
                       content=request.content, headers=dict(request.headers))
        return httpx.Response(r.status_code, content=r.content,
                              headers=dict(r.headers))
    return httpx.MockTransport(handler)


@pytest.fixture(scope="module")
def transport():
    app = create_app(services_path=ROOT_SERVICES, secret=SECRET)
    return _asgi_transport(app)


def _adapter(transport, service_id: str) -> WebhookAdapter:
    """A live webhook adapter for one service, routed at the backend in-process."""
    svc = load_services(ROOT_SERVICES).services  # mock defs -> reuse the schema
    service = next(s for s in svc if s.id == service_id)
    service.mode = Mode.live
    service.live = LiveConfig(transport="webhook",
                              base_url=f"http://backend/{service_id}",
                              auth=SECRET, timeout_ms=2000)
    return WebhookAdapter(service=service, transport=transport)


def test_scheduling_full_flow_over_webhook(transport):
    a = _adapter(transport, "sales-scheduler")
    slot = a.invoke("get_availability", {"date": "2026-07-09"})["slots"][0]
    hold = a.invoke("hold_slot", {"slot_id": slot["slot_id"], "interaction_token": "itx_t"})
    booking = a.invoke("confirm_booking", {"hold_id": hold["hold_id"], "interaction_token": "itx_t"})
    assert booking["start"] == slot["start"]
    assert a.invoke("cancel_booking",
                    {"booking_ref": booking["booking_ref"], "interaction_token": "itx_t"}) \
        == {"cancelled": True}


def test_tokenless_write_refused(transport):
    a = _adapter(transport, "sales-scheduler")
    slot = a.invoke("get_availability", {"date": "2026-07-09"})["slots"][0]
    with pytest.raises(InteractionTokenRequired):
        a.invoke("hold_slot", {"slot_id": slot["slot_id"]})


def test_crm_capture_lead_over_webhook(transport):
    a = _adapter(transport, "sales-crm")
    out = a.invoke("capture_lead",
                   {"values": {"name": "Ada", "email": "ada@example.com"},
                    "interaction_token": "itx_t"})
    assert out["lead_ref"].startswith("ld_")


def test_custom_order_lookup_over_webhook(transport):
    a = _adapter(transport, "order-lookup")
    assert a.invoke("get_order_status", {"order_ref": "A1001"})["status"] == "shipped"


def test_bad_signature_rejected(transport):
    with httpx.Client(transport=transport, base_url="http://backend") as c:
        r = c.post("/sales-scheduler/get_availability",
                   json={"args": {"date": "2026-07-09"}},
                   headers={"X-Webchat-Timestamp": "1", "X-Webchat-Signature": "v1=bad"})
        assert r.status_code == 401


def test_unknown_service_404(transport):
    a = ServicesFile.model_validate({
        "version": 1, "tenant": "t",
        "services": [{"id": "nope-svc", "kind": "scheduling", "mode": "live",
                      "description": "x", "capabilities": ["get_availability"],
                      "live": {"transport": "webhook", "base_url": "http://backend/nope-svc",
                               "auth": SECRET, "timeout_ms": 2000}}]}).services[0]
    adapter = WebhookAdapter(service=a, transport=transport)
    with pytest.raises(Exception):
        adapter.invoke("get_availability", {"date": "2026-07-09"})
