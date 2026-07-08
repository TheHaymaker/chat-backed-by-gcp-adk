"""Shared plumbing for live transports (webhook / mcp / openapi).

Untrusted-tool discipline, enforced below every transport:
  - per-service timeout (live.timeout_ms) and output size cap
  - circuit breaker: consecutive failures open the circuit for a cooldown,
    so one bad tenant endpoint can't stall every turn
  - the human-tap rule holds on the way OUT too: write capabilities refuse
    to fire without a well-formed interaction token, live or mock
  - secrets resolve at call time from env:// or secret-manager:// refs

Tool OUTPUT screening (Model Armor after_tool) happens above this layer.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..config import ServiceConfig

MAX_OUTPUT_BYTES = 64 * 1024


class LiveError(Exception):
    """Contract-level failure from a live transport (surfaced to the model)."""


class InteractionTokenRequired(LiveError):
    pass


class CircuitOpen(LiveError):
    pass


def resolve_secret(ref: str | None) -> str | None:
    """env://VAR | secret-manager://name[/version] | literal (dev only)."""
    if not ref:
        return None
    if ref.startswith("env://"):
        return os.environ[ref[6:]]
    if ref.startswith("secret-manager://"):
        from google.cloud import secretmanager
        name = ref[len("secret-manager://"):]
        if "/" not in name:
            project = os.environ["GOOGLE_CLOUD_PROJECT"]
            name = f"projects/{project}/secrets/{name}/versions/latest"
        client = secretmanager.SecretManagerServiceClient()
        return client.access_secret_version(name=name).payload.data.decode()
    return ref


@dataclass
class CircuitBreaker:
    threshold: int = 3
    cooldown_s: float = 30.0
    now: callable = time.monotonic
    _failures: int = 0
    _opened_at: float | None = None

    def check(self) -> None:
        if self._opened_at is not None:
            if self.now() - self._opened_at < self.cooldown_s:
                raise CircuitOpen("service temporarily unavailable (circuit open)")
            self._opened_at = None          # half-open: allow a probe
            self._failures = self.threshold - 1

    def record(self, ok: bool) -> None:
        if ok:
            self._failures, self._opened_at = 0, None
        else:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = self.now()


@dataclass
class LiveAdapterBase:
    service: ServiceConfig
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    @property
    def timeout_s(self) -> float:
        return (self.service.live.timeout_ms if self.service.live else 5000) / 1000.0

    def _require_token_for_writes(self, capability: str, args: dict) -> None:
        try:
            is_write = self.service.is_write(capability)
        except KeyError:
            is_write = False
        if is_write:
            token = args.get("interaction_token")
            if not isinstance(token, str) or not token.startswith("itx_"):
                raise InteractionTokenRequired(
                    f"{self.service.id}.{capability} is a write tool and requires "
                    "an interaction_token from a verified user tap")

    def _cap_output(self, body: bytes) -> None:
        if len(body) > MAX_OUTPUT_BYTES:
            raise LiveError(
                f"{self.service.id}: response exceeds {MAX_OUTPUT_BYTES} byte cap")

    def guarded(self, capability: str, args: dict, fn):
        """Breaker + token rule around a transport call."""
        self._require_token_for_writes(capability, args)
        self.breaker.check()
        try:
            result = fn()
        except InteractionTokenRequired:
            raise
        except LiveError:
            self.breaker.record(False)
            raise
        except Exception as e:
            self.breaker.record(False)
            raise LiveError(f"{self.service.id}.{capability} failed: {e}") from e
        self.breaker.record(True)
        return result
