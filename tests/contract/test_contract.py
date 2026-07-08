"""Contract suite: the scheduling contract must hold identically for the mock
adapter and the live webhook path (adapter -> HMAC wire -> reference backend).

This is the Phase 6 gate: a service may flip `mode: live` only when this suite
passes against its real endpoint — swap the ASGI transport for the staging URL.
"""
import httpx
import pytest

from agent.registry.config import ServicesFile
from agent.registry.live.base import (
    CircuitBreaker,
    CircuitOpen,
    InteractionTokenRequired,
    LiveError,
)
from agent.registry.live.webhook import WebhookAdapter, sign, verify
from agent.registry.mocks.scheduling import MockSchedulingAdapter
from agent.registry.mocks.scheduling import InteractionTokenRequired as MockTokenRequired
from tests.contract.reference_backend import build_reference_backend

SECRET = "contract-test-secret"


def sync_asgi_transport(app) -> httpx.MockTransport:
    """Bridge a sync httpx.Client to an ASGI app (ASGITransport is async-only)."""
    from starlette.testclient import TestClient
    tc = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        r = tc.request(request.method, request.url.path,
                       content=request.content,
                       headers=dict(request.headers))
        return httpx.Response(r.status_code, content=r.content,
                              headers=dict(r.headers))
    return httpx.MockTransport(handler)


def _service(mode: str) -> object:
    raw = {"version": 1, "tenant": "t", "services": [{
        "id": "sched-x", "kind": "scheduling", "mode": mode,
        "description": "contract test scheduler",
        "capabilities": ["get_availability", "hold_slot",
                          "confirm_booking", "cancel_booking"],
        "live": {"transport": "webhook", "base_url": "http://backend",
                 "auth": SECRET, "timeout_ms": 2000},
    }]}
    return ServicesFile.model_validate(raw).services[0]


class MockFacade:
    """Adapts the mock to the same invoke(name, args) surface for the suite."""
    def __init__(self, svc):
        self._a = MockSchedulingAdapter(service=svc)
        self.token_error = MockTokenRequired

    def invoke(self, cap, args):
        if cap == "get_availability":
            return self._a.get_availability(args["date"],
                                            args.get("duration_minutes", 30))
        return getattr(self._a, cap)(**args)


class LiveFacade:
    def __init__(self, svc):
        backend = build_reference_backend(svc, SECRET)
        transport = sync_asgi_transport(backend)
        self._a = WebhookAdapter(service=svc, transport=transport)
        self.token_error = InteractionTokenRequired

    def invoke(self, cap, args):
        return self._a.invoke(cap, args)


@pytest.fixture(params=["mock", "live"])
def adapter(request):
    if request.param == "mock":
        return MockFacade(_service("mock"))
    return LiveFacade(_service("live"))


# -- the contract, asserted identically on both paths ------------------------------

def test_contract_availability_shape(adapter):
    out = adapter.invoke("get_availability", {"date": "2026-07-09"})
    assert out["timezone"] == "UTC"
    assert out["slots"] and {"slot_id", "start", "end"} <= set(out["slots"][0])


def test_contract_full_booking_flow(adapter):
    slot = adapter.invoke("get_availability", {"date": "2026-07-09"})["slots"][0]
    hold = adapter.invoke("hold_slot", {"slot_id": slot["slot_id"],
                                        "interaction_token": "itx_ct"})
    booking = adapter.invoke("confirm_booking", {"hold_id": hold["hold_id"],
                                                 "interaction_token": "itx_ct"})
    assert booking["start"] == slot["start"]
    assert adapter.invoke("cancel_booking",
                          {"booking_ref": booking["booking_ref"],
                           "interaction_token": "itx_ct"}) == {"cancelled": True}


def test_contract_write_requires_token(adapter):
    slot = adapter.invoke("get_availability", {"date": "2026-07-09"})["slots"][0]
    with pytest.raises(adapter.token_error):
        adapter.invoke("hold_slot", {"slot_id": slot["slot_id"]})
    with pytest.raises(adapter.token_error):
        adapter.invoke("hold_slot", {"slot_id": slot["slot_id"],
                                     "interaction_token": "forged"})


def test_contract_unknown_hold_fails(adapter):
    with pytest.raises(Exception):
        adapter.invoke("confirm_booking", {"hold_id": "hold_nope",
                                           "interaction_token": "itx_ct"})


# -- webhook wire security ------------------------------------------------------------

def test_backend_rejects_bad_signature():
    svc = _service("live")
    backend = build_reference_backend(svc, SECRET)
    transport = sync_asgi_transport(backend)
    with httpx.Client(transport=transport, base_url="http://backend") as c:
        r = c.post("/get_availability", json={"args": {"date": "2026-07-09"}},
                   headers={"X-Webchat-Timestamp": "1", "X-Webchat-Signature": "v1=bad"})
        assert r.status_code == 401


def test_signature_roundtrip_and_skew():
    import time
    body = b'{"args":{}}'
    ts = str(int(time.time()))
    assert verify(SECRET, ts, body, sign(SECRET, ts, body))
    assert not verify(SECRET, "1000", body, sign(SECRET, "1000", body))  # stale
    assert not verify("other", ts, body, sign(SECRET, ts, body))


# -- circuit breaker ----------------------------------------------------------------

def test_circuit_breaker_opens_and_half_opens():
    clock = {"t": 0.0}
    br = CircuitBreaker(threshold=3, cooldown_s=30, now=lambda: clock["t"])
    for _ in range(3):
        br.check(); br.record(False)
    with pytest.raises(CircuitOpen):
        br.check()
    clock["t"] = 31.0
    br.check()                 # half-open probe allowed
    br.record(True)
    br.check()                 # closed again


def test_webhook_failures_trip_breaker():
    svc = _service("live")
    def exploding(request):
        return httpx.Response(500, text="boom")
    a = WebhookAdapter(service=svc,
                       transport=httpx.MockTransport(exploding))
    for _ in range(3):
        with pytest.raises(LiveError):
            a.invoke("get_availability", {"date": "2026-07-09"})
    with pytest.raises(CircuitOpen):
        a.invoke("get_availability", {"date": "2026-07-09"})


def test_output_cap_enforced():
    svc = _service("live")
    def huge(request):
        return httpx.Response(200, json={"result": {"x": "a" * 70_000}})
    a = WebhookAdapter(service=svc, transport=httpx.MockTransport(huge))
    with pytest.raises(LiveError, match="byte cap"):
        a.invoke("get_availability", {"date": "2026-07-09"})
