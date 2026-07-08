"""Reference webhook backend — the tenant side of the webhook protocol.

A FastAPI app implementing the scheduling contract over the webhook wire
format, delegating to MockSchedulingAdapter (the canonical contract
implementation). It verifies HMAC signatures exactly as the tenant onboarding
kit tells tenants to, making the contract suite a two-sided test: our adapter
signs correctly AND a correctly-implemented tenant accepts it.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.registry.config import ServiceConfig
from agent.registry.live.webhook import verify
from agent.registry.mocks.scheduling import MockSchedulingAdapter, SchedulingError


def build_reference_backend(service: ServiceConfig, secret: str) -> FastAPI:
    app = FastAPI(title="reference-webhook-backend")
    backend = MockSchedulingAdapter(service=service)
    app.state.backend = backend

    @app.post("/{capability}")
    async def call(capability: str, request: Request):
        body = await request.body()
        ts = request.headers.get("x-webchat-timestamp", "0")
        sig = request.headers.get("x-webchat-signature", "")
        if not verify(secret, ts, body, sig):
            return JSONResponse({"error": "bad signature"}, status_code=401)
        args = (await request.json()).get("args", {})
        method = getattr(backend, capability, None)
        if method is None:
            return JSONResponse({"error": f"unknown capability {capability}"},
                                status_code=404)
        try:
            if capability == "get_availability":
                result = method(args["date"], args.get("duration_minutes", 30))
            else:
                result = method(**args)
        except SchedulingError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        return {"result": result}

    return app
