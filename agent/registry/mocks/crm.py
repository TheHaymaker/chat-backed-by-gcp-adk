"""Mock adapter for kind: crm — lead capture.

capture_lead is a WRITE capability: it fires only with a client interaction
token minted by the gateway when the user actually submitted a form. The mock
enforces this (like scheduling does) so the safety property is tested from
day one. Live swap targets: HubSpot/Salesforce via MCP, or a tenant webhook.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from ..config import ServiceConfig

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CrmError(Exception):
    pass


class InteractionTokenRequired(CrmError):
    pass


@dataclass
class MockCrmAdapter:
    service: ServiceConfig
    _leads: dict[str, dict] = field(default_factory=dict)

    def capture_lead(self, values: dict, interaction_token: str | None = None) -> dict:
        if not interaction_token or not interaction_token.startswith("itx_"):
            raise InteractionTokenRequired(
                "capture_lead requires a client interaction_token (form submit)")
        if not isinstance(values, dict) or not values.get("email"):
            raise CrmError("lead requires at least an email")
        if not _EMAIL.match(str(values["email"])):
            raise CrmError("invalid email address")
        ref = "ld_" + hashlib.sha256(
            json.dumps(values, sort_keys=True).encode()).hexdigest()[:8]
        self._leads[ref] = dict(values)          # idempotent on identical payloads
        return {"lead_ref": ref}
