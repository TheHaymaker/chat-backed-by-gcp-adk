"""MCP and OpenAPI transports.

MCP: minimal JSON-RPC 2.0 client over streamable HTTP — initialize once, then
tools/call. Tool names are filtered by the manifest `allow` list, and results
use the standard content-parts shape. (Inside the ADK agent you can also mount
a tenant MCP server directly as an McpToolset; this client exists so the same
service works through the registry seam and the contract-test suite.)

OpenAPI: maps capabilities to operationIds in the tenant's spec. GET operations
send args as query params; everything else as a JSON body. The spec is fetched
once and cached per adapter.
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field

import httpx

from .base import LiveAdapterBase, LiveError, resolve_secret


@dataclass
class McpAdapter(LiveAdapterBase):
    transport: httpx.BaseTransport | None = None
    _client: httpx.Client | None = field(default=None, repr=False)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _initialized: bool = False
    allow: list[str] | None = None      # populated from manifest extras

    def _http(self) -> httpx.Client:
        if self._client is None:
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json"}
            token = resolve_secret(self.service.live.auth) \
                if self.service.live.auth not in (None, "oidc") else None
            if token:
                headers["Authorization"] = f"Bearer {token}"
            elif self.service.live.auth == "oidc":
                headers["Authorization"] = f"Bearer {self._id_token()}"
            self._client = httpx.Client(timeout=self.timeout_s,
                                        transport=self.transport,
                                        headers=headers)
        return self._client

    def _id_token(self) -> str:
        import google.auth.transport.requests
        import google.oauth2.id_token
        req = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(req, self.service.live.url)

    def _rpc(self, method: str, params: dict) -> dict:
        r = self._http().post(self.service.live.url, json={
            "jsonrpc": "2.0", "id": next(self._ids),
            "method": method, "params": params})
        self._cap_output(r.content)
        if r.status_code != 200:
            raise LiveError(f"mcp HTTP {r.status_code}")
        payload = r.json()
        if "error" in payload:
            raise LiveError(f"mcp error: {payload['error'].get('message')}")
        return payload.get("result", {})

    def _ensure_init(self) -> None:
        if not self._initialized:
            self._rpc("initialize", {
                "protocolVersion": "2025-03-26",
                "clientInfo": {"name": "web-chat-agent", "version": "1.0"},
                "capabilities": {}})
            self._initialized = True

    def invoke(self, capability: str, args: dict) -> dict:
        if self.allow is not None and capability not in self.allow:
            raise LiveError(f"tool '{capability}' not in the allow list for "
                            f"'{self.service.id}'")
        def _call() -> dict:
            self._ensure_init()
            result = self._rpc("tools/call",
                               {"name": capability, "arguments": args})
            if result.get("isError"):
                raise LiveError(f"mcp tool {capability} reported an error")
            texts = [c.get("text", "") for c in result.get("content", [])
                     if c.get("type") == "text"]
            joined = "\n".join(t for t in texts if t)
            try:
                return json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                return {"text": joined}
        return self.guarded(capability, args, _call)


@dataclass
class OpenApiAdapter(LiveAdapterBase):
    transport: httpx.BaseTransport | None = None
    spec: dict | None = None            # injectable; else fetched from spec_url
    _client: httpx.Client | None = field(default=None, repr=False)
    _ops: dict | None = field(default=None, repr=False)

    def _http(self) -> httpx.Client:
        if self._client is None:
            headers = {}
            token = resolve_secret(self.service.live.auth)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._client = httpx.Client(timeout=self.timeout_s,
                                        transport=self.transport,
                                        headers=headers)
        return self._client

    def _operations(self) -> dict:
        if self._ops is None:
            if self.spec is None:
                r = self._http().get(self.service.live.spec_url)
                if r.status_code != 200:
                    raise LiveError(f"openapi spec fetch -> HTTP {r.status_code}")
                self.spec = r.json()
            self._ops = {}
            for path, methods in (self.spec.get("paths") or {}).items():
                for verb, op in methods.items():
                    if isinstance(op, dict) and op.get("operationId"):
                        self._ops[op["operationId"]] = (verb.upper(), path)
        return self._ops

    def invoke(self, capability: str, args: dict) -> dict:
        def _call() -> dict:
            ops = self._operations()
            if capability not in ops:
                raise LiveError(f"operationId '{capability}' not found in spec "
                                f"for '{self.service.id}'")
            verb, path = ops[capability]
            base = (self.spec.get("servers") or [{}])[0].get(
                "url", "") or self.service.live.base_url or ""
            # substitute {pathParams} from args
            body = dict(args)
            for key in list(body):
                if "{" + key + "}" in path:
                    path = path.replace("{" + key + "}", str(body.pop(key)))
            url = base.rstrip("/") + path
            r = (self._http().get(url, params=body) if verb == "GET"
                 else self._http().request(verb, url, json=body))
            self._cap_output(r.content)
            if r.status_code >= 400:
                raise LiveError(f"openapi {capability} -> HTTP {r.status_code}")
            return r.json()
        return self.guarded(capability, args, _call)
