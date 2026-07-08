"""Standalone multi-kind placeholder ("tenant") backend for live-mode services.

Stands up REAL HTTP endpoints implementing the same contracts as the mock
adapters, over the webhook wire format (HMAC-signed). Point a service's
``live.base_url`` at ``http://<host>/<service_id>`` and flip ``mode: live`` to
exercise the full live transport path end to end — proving scheduling / CRM /
custom tool calls actually work over a network hop, not just as in-process mocks.

This generalizes ``tests/contract/reference_backend.py`` from a single
scheduling service to every write/tool kind. Rather than re-implement the
contracts, it drives ``ToolRegistry.invoke()`` over the *mock* ``services.yaml``
— so the tenant side behaves byte-for-byte like the agent's mock path, and the
interaction-token / write rules are enforced here too (defence in depth).

Wire protocol (see agent/registry/live/webhook.py):
    POST /{service_id}/{capability}
    Headers: X-Webchat-Timestamp, X-Webchat-Signature: v1=<hmac-sha256>
    Body:    {"args": {...}}
    Reply:   200 {"result": {...}}  |  401 bad sig  |  404 unknown  |  422 contract error

Run it:
    WEBHOOK_SECRET=dev-secret SERVICES_CONFIG=services.yaml \
        uvicorn placeholders.backend:create_app --factory --port 8090
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.registry.config import Mode, load_services
from agent.registry.live.webhook import verify
from agent.registry.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]

# Domain (contract-level) errors from the mock adapters -> HTTP 422; anything
# unknown (bad service/capability) -> 404. Imported lazily-safe at module load.
from agent.registry.mocks.crm import CrmError
from agent.registry.mocks.custom import CustomToolError
from agent.registry.mocks.scheduling import SchedulingError

_CONTRACT_ERRORS = (SchedulingError, CrmError, CustomToolError, ValueError)


def create_app(services_path: str | Path | None = None,
               secret: str | None = None) -> FastAPI:
    services_path = Path(services_path
                         or os.environ.get("SERVICES_CONFIG", ROOT / "services.yaml"))
    secret = secret or os.environ.get("WEBHOOK_SECRET", "")
    services = load_services(services_path)
    # The tenant side runs the canonical mock adapters regardless of what `mode`
    # the shipped file declares — this backend *is* the fake booking/CRM system.
    for svc in services.services:
        svc.mode = Mode.mock
    registry = ToolRegistry(services=services, base_dir=services_path.parent)
    known_ids = {s.id for s in services.services}

    app = FastAPI(title="webchat-placeholders")
    app.state.registry = registry

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "services": sorted(known_ids)}

    @app.post("/{service_id}/{capability}")
    async def call(service_id: str, capability: str, request: Request):
        body = await request.body()
        ts = request.headers.get("x-webchat-timestamp", "0")
        sig = request.headers.get("x-webchat-signature", "")
        if not secret or not verify(secret, ts, body, sig):
            return JSONResponse({"error": "bad signature"}, status_code=401)
        if service_id not in known_ids:
            return JSONResponse({"error": f"unknown service {service_id}"},
                                status_code=404)
        try:
            args = (await request.json()).get("args", {}) or {}
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        try:
            result = registry.invoke(service_id, capability, **args)
        except (AttributeError, KeyError):
            return JSONResponse(
                {"error": f"unknown capability {service_id}.{capability}"},
                status_code=404)
        except _CONTRACT_ERRORS as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        return {"result": result}

    return app
