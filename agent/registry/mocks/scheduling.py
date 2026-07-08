"""Mock adapter for kind: scheduling.

Implements the canonical scheduling contract (contracts/kinds/scheduling.v1.json)
with deterministic, fixture-free behavior so evals are stable:

  - Slots are generated from sha256(service_id + date): same inputs -> same slots.
  - hold_slot / confirm_booking / cancel_booking enforce the interaction-token
    rule even in mock mode (the model can propose; only a user tap can commit).
  - Holds expire after HOLD_TTL_SECONDS; confirming an expired or unknown hold
    fails the same way a live backend would.
  - Optional chaos knobs (latency, failure_rate) come from MockConfig.
"""
from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..config import MockConfig, ServiceConfig

HOLD_TTL_SECONDS = 300


class SchedulingError(Exception):
    """Raised for contract-level failures (bad token, expired hold, chaos)."""


class InteractionTokenRequired(SchedulingError):
    pass


@dataclass
class _Hold:
    slot_id: str
    expires_at: float


@dataclass
class MockSchedulingAdapter:
    service: ServiceConfig
    now: callable = field(default=lambda: time.time())  # injectable clock for tests
    _holds: dict[str, _Hold] = field(default_factory=dict)
    _bookings: dict[str, dict] = field(default_factory=dict)

    # -- internal helpers ---------------------------------------------------
    @property
    def _cfg(self) -> MockConfig:
        return self.service.mock

    def _chaos(self) -> None:
        if self._cfg.latency_ms:
            time.sleep(self._cfg.latency_ms / 1000.0)
        if self._cfg.failure_rate > 0:
            rng = random.Random(self._cfg.seed)
            if rng.random() < self._cfg.failure_rate:
                raise SchedulingError("mock backend failure (chaos)")

    def _require_token(self, token: str | None) -> None:
        # Real verification (signature, turn binding) happens in the gateway;
        # the adapter enforces presence + shape so the safety property is
        # exercised in every eval, mock or live.
        if not token or not token.startswith("itx_"):
            raise InteractionTokenRequired(
                "write operation requires a client interaction_token (user tap)"
            )

    def _seed(self, day: str) -> int:
        digest = hashlib.sha256(f"{self.service.id}:{day}".encode()).hexdigest()
        return int(digest[:12], 16)

    # -- contract capabilities ----------------------------------------------
    def list_services(self) -> dict:
        self._chaos()
        return {
            "services": [
                {"name": self.service.id, "duration_minutes": 30},
                {"name": f"{self.service.id}-extended", "duration_minutes": 60},
            ]
        }

    def get_availability(self, date_str: str, duration_minutes: int = 30) -> dict:
        self._chaos()
        day = date.fromisoformat(date_str)
        rng = random.Random(self._cfg.seed if self._cfg.seed is not None else self._seed(date_str))
        # Business hours 09:00-17:00 UTC; deterministic subset of half-hour starts.
        starts = [9 * 60 + 30 * i for i in range(16)]
        available = sorted(rng.sample(starts, k=rng.randint(4, 8)))
        slots = []
        for minutes in available:
            start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(minutes=minutes)
            end = start + timedelta(minutes=duration_minutes)
            slot_id = f"{self.service.id}:{date_str}:{minutes}"
            slots.append({
                "slot_id": slot_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
            })
        return {"timezone": "UTC", "slots": slots}

    def hold_slot(self, slot_id: str, interaction_token: str | None = None) -> dict:
        self._chaos()
        self._require_token(interaction_token)
        if not slot_id.startswith(f"{self.service.id}:"):
            raise SchedulingError(f"unknown slot_id '{slot_id}' for service '{self.service.id}'")
        hold_id = f"hold_{hashlib.sha256(slot_id.encode()).hexdigest()[:10]}"
        expires = self.now() + HOLD_TTL_SECONDS
        self._holds[hold_id] = _Hold(slot_id=slot_id, expires_at=expires)
        return {
            "hold_id": hold_id,
            "expires_at": datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
        }

    def confirm_booking(self, hold_id: str, interaction_token: str | None = None,
                        contact: dict | None = None) -> dict:
        self._chaos()
        self._require_token(interaction_token)
        hold = self._holds.get(hold_id)
        if hold is None:
            raise SchedulingError(f"unknown hold '{hold_id}'")
        if self.now() > hold.expires_at:
            del self._holds[hold_id]
            raise SchedulingError(f"hold '{hold_id}' expired; re-check availability")
        _, day, minutes = hold.slot_id.split(":")
        start = datetime.fromisoformat(day + "T00:00:00+00:00") + timedelta(minutes=int(minutes))
        booking_ref = f"bk_{hashlib.sha256(hold_id.encode()).hexdigest()[:8]}"
        booking = {
            "booking_ref": booking_ref,
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
        }
        self._bookings[booking_ref] = booking
        del self._holds[hold_id]
        return booking

    def cancel_booking(self, booking_ref: str, interaction_token: str | None = None) -> dict:
        self._chaos()
        self._require_token(interaction_token)
        if booking_ref not in self._bookings:
            raise SchedulingError(f"unknown booking '{booking_ref}'")
        del self._bookings[booking_ref]
        return {"cancelled": True}
