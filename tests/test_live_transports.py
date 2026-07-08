"""MCP and OpenAPI transport tests against fake servers (httpx.MockTransport)."""
import json

import httpx
import pytest

from agent.registry.config import ServicesFile
from agent.registry.live.base import LiveError
from agent.registry.live.mcp_openapi import McpAdapter, OpenApiAdapter


def _svc(transport: str, **live_extra):
    raw = {"version": 1, "tenant": "t", "services": [{
        "id": "byo-x", "kind": "custom", "mode": "live",
        "description": "byo tool",
        "tools": [{"name": "get_order_status",
                   "input_schema": {"type": "object", "required": ["order_ref"],
                                    "properties": {"order_ref": {"type": "string"}}}},
                  {"name": "delete_order", "write": True,
                   "input_schema": {"type": "object", "properties": {}}}],
        "live": {"transport": transport, "timeout_ms": 2000, **live_extra},
    }]}
    return ServicesFile.model_validate(raw).services[0]


# -- MCP -----------------------------------------------------------------------

def _fake_mcp(request: httpx.Request) -> httpx.Response:
    req = json.loads(request.content)
    if req["method"] == "initialize":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": req["id"],
            "result": {"protocolVersion": "2025-03-26", "capabilities": {}}})
    if req["method"] == "tools/call":
        name = req["params"]["name"]
        if name == "get_order_status":
            body = json.dumps({"status": "shipped",
                               "order_ref": req["params"]["arguments"]["order_ref"]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req["id"],
                "result": {"content": [{"type": "text", "text": body}]}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": req["id"],
            "result": {"isError": True, "content": []}})
    return httpx.Response(400)


def test_mcp_call_roundtrip():
    a = McpAdapter(service=_svc("mcp", url="http://mcp.fake/mcp"),
                   transport=httpx.MockTransport(_fake_mcp))
    out = a.invoke("get_order_status", {"order_ref": "A1"})
    assert out == {"status": "shipped", "order_ref": "A1"}


def test_mcp_allow_list_blocks_unlisted_tools():
    a = McpAdapter(service=_svc("mcp", url="http://mcp.fake/mcp"),
                   transport=httpx.MockTransport(_fake_mcp),
                   allow=["get_order_status"])
    with pytest.raises(LiveError, match="allow list"):
        a.invoke("some_other_tool", {})


def test_mcp_write_tool_requires_token():
    a = McpAdapter(service=_svc("mcp", url="http://mcp.fake/mcp"),
                   transport=httpx.MockTransport(_fake_mcp))
    from agent.registry.live.base import InteractionTokenRequired
    with pytest.raises(InteractionTokenRequired):
        a.invoke("delete_order", {})


def test_mcp_tool_error_surfaces():
    a = McpAdapter(service=_svc("mcp", url="http://mcp.fake/mcp"),
                   transport=httpx.MockTransport(_fake_mcp))
    with pytest.raises(LiveError):
        a.invoke("get_order_status_bad", {"order_ref": "A1"})


# -- OpenAPI --------------------------------------------------------------------

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "http://api.fake"}],
    "paths": {
        "/orders/{order_ref}": {"get": {"operationId": "get_order_status"}},
        "/orders/{order_ref}/refund": {"post": {"operationId": "refund_order"}},
    },
}


def _fake_api(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/spec.json":
        return httpx.Response(200, json=SPEC)
    if request.url.path == "/orders/A1" and request.method == "GET":
        return httpx.Response(200, json={"status": "shipped",
                                         "verbose": request.url.params.get("verbose")})
    if request.url.path == "/orders/A1/refund" and request.method == "POST":
        return httpx.Response(200, json={"refunded": True})
    return httpx.Response(404)


def test_openapi_get_with_path_and_query_args():
    a = OpenApiAdapter(service=_svc("openapi", spec_url="http://api.fake/spec.json"),
                       transport=httpx.MockTransport(_fake_api))
    out = a.invoke("get_order_status", {"order_ref": "A1", "verbose": "1"})
    assert out == {"status": "shipped", "verbose": "1"}


def test_openapi_unknown_operation():
    a = OpenApiAdapter(service=_svc("openapi", spec_url="http://api.fake/spec.json"),
                       transport=httpx.MockTransport(_fake_api))
    with pytest.raises(LiveError, match="not found in spec"):
        a.invoke("nonexistent_op", {})


def test_openapi_http_error_maps_to_live_error():
    a = OpenApiAdapter(service=_svc("openapi", spec_url="http://api.fake/spec.json"),
                       transport=httpx.MockTransport(_fake_api))
    with pytest.raises(LiveError):
        a.invoke("get_order_status", {"order_ref": "NOPE"})


# -- secrets -----------------------------------------------------------------------

def test_env_secret_resolution(monkeypatch):
    from agent.registry.live.base import resolve_secret
    monkeypatch.setenv("HOOK_KEY", "s3cret")
    assert resolve_secret("env://HOOK_KEY") == "s3cret"
    assert resolve_secret("literal-dev-key") == "literal-dev-key"
    assert resolve_secret(None) is None
