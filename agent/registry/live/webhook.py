"""Webhook transport — the low-floor BYO integration.

Protocol (documented for tenants in the onboarding kit):
  POST {base_url}/{capability}
  Headers:
    Content-Type: application/json
    X-Webchat-Timestamp: <unix seconds>
    X-Webchat-Signature: v1=<hex hmac-sha256(secret, "{ts}.{body}")>
  Body:    {"args": {...}}                # includes interaction_token for writes
  Reply:   200 {"result": {...}}          # anything else is a LiveError
Tenants MUST verify the signature and reject stale timestamps (>5 min skew).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

import httpx

from .base import LiveAdapterBase, LiveError, resolve_secret


def sign(secret: str, ts: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return f"v1={mac.hexdigest()}"


def verify(secret: str, ts: str, body: bytes, signature: str,
           max_skew_s: int = 300) -> bool:
    """Reference verifier — shipped to tenants and used by our test backend."""
    if abs(time.time() - float(ts)) > max_skew_s:
        return False
    return hmac.compare_digest(sign(secret, ts, body), signature)


@dataclass
class WebhookAdapter(LiveAdapterBase):
    transport: httpx.BaseTransport | None = None    # injectable for tests
    _client: httpx.Client | None = field(default=None, repr=False)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s,
                                        transport=self.transport)
        return self._client

    def invoke(self, capability: str, args: dict) -> dict:
        def _call() -> dict:
            live = self.service.live
            secret = resolve_secret(live.auth)
            if not secret:
                raise LiveError(f"{self.service.id}: webhook requires an hmac secret")
            body = json.dumps({"args": args}, separators=(",", ":")).encode()
            ts = str(int(time.time()))
            r = self._http().post(
                f"{live.base_url.rstrip('/')}/{capability}",
                content=body,
                headers={"Content-Type": "application/json",
                         "X-Webchat-Timestamp": ts,
                         "X-Webchat-Signature": sign(secret, ts, body)})
            self._cap_output(r.content)
            if r.status_code != 200:
                raise LiveError(f"webhook {capability} -> HTTP {r.status_code}: "
                                f"{r.text[:200]}")
            payload = r.json()
            if "result" not in payload:
                raise LiveError(f"webhook {capability}: reply missing 'result'")
            return payload["result"]
        return self.guarded(capability, args, _call)
