"""Gateway security primitives.

Two token types, both HMAC-SHA256, no external deps:

1. Session tokens (`st_`): minted at widget boot for anonymous visitors.
   Claims: tenant, session id, expiry. No PII. Sent as Authorization: Bearer.

2. Interaction tokens (`itx_`): the human-tap rule. Minted ONLY when the widget
   reports a user interaction on a UI block (e.g. slot tap), bound to
   (session, action, payload-hash) with a short TTL. The agent must present one
   to any write tool; adapters check shape, the gateway checks the signature —
   a model-fabricated token can never verify.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TokenError(Exception):
    pass


@dataclass
class TokenService:
    secret: bytes
    session_ttl: int = 60 * 60          # 1h anonymous session
    interaction_ttl: int = 120          # 2m to act on a tap

    # -- generic signed payload ------------------------------------------------
    def _sign(self, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        sig = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def _verify(self, token: str) -> dict:
        try:
            body, sig = token.split(".")
        except ValueError as e:
            raise TokenError("malformed token") from e
        expected = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise TokenError("bad signature")
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            raise TokenError("expired")
        return payload

    # -- session tokens ---------------------------------------------------------
    def mint_session(self, tenant: str, session_id: str) -> str:
        return "st_" + self._sign({
            "t": tenant, "sid": session_id, "exp": int(time.time()) + self.session_ttl,
        })

    def verify_session(self, token: str) -> dict:
        if not token.startswith("st_"):
            raise TokenError("not a session token")
        return self._verify(token[3:])

    # -- interaction tokens (human-tap rule) --------------------------------------
    def mint_interaction(self, session_id: str, action: str, payload: dict) -> str:
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:16]
        return "itx_" + self._sign({
            "sid": session_id, "act": action, "ph": payload_hash,
            "exp": int(time.time()) + self.interaction_ttl,
        })

    def verify_interaction(self, token: str, session_id: str, action: str, payload: dict) -> None:
        if not token.startswith("itx_"):
            raise TokenError("not an interaction token")
        claims = self._verify(token[4:])
        if claims["sid"] != session_id:
            raise TokenError("session mismatch")
        if claims["act"] != action:
            raise TokenError("action mismatch")
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:16]
        if not hmac.compare_digest(claims["ph"], payload_hash):
            raise TokenError("payload mismatch")
