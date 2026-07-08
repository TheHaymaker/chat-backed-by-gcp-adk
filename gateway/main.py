"""Chat Gateway (BFF) — FastAPI app.

Endpoints:
  POST /v1/session    -> mint anonymous session token (origin-allowlisted)
  GET  /v1/config     -> widget capability negotiation (enabled blocks, registry v1)
  POST /v1/chat       -> user turn; SSE stream of validated envelopes
  POST /v1/interact   -> user tap on a UI block; gateway verifies+forwards with
                         a freshly minted interaction token (human-tap rule)
  POST /v1/events     -> batched widget analytics events -> sink

Cloud Run-ready: stateless; secret from env; Cloud Armor/LB sits in front.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.registry.config import ServicesFile, load_services
from agent.registry.registry import ToolRegistry
from gateway.runner import AgentRunner, EventSink, LogSink, MockAgentRunner
from gateway.security import TokenError, TokenService
from gateway.validation import EnvelopeValidator

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class GatewayState:
    tokens: TokenService
    validator: EnvelopeValidator
    services: ServicesFile
    registry: ToolRegistry
    runner: AgentRunner
    sink: EventSink
    allowed_origins: set[str]


def build_state(
    services_path: str | Path | None = None,
    runner: AgentRunner | None = None,
    sink: EventSink | None = None,
    allowed_origins: set[str] | None = None,
) -> GatewayState:
    services_path = Path(services_path or os.environ.get("SERVICES_CONFIG", ROOT / "services.yaml"))
    services = load_services(services_path)
    registry = ToolRegistry(services=services, base_dir=services_path.parent)
    return GatewayState(
        tokens=TokenService(secret=os.environ.get("GATEWAY_SECRET", "dev-secret").encode()),
        validator=EnvelopeValidator(contracts_dir=ROOT / "contracts"),
        services=services,
        registry=registry,
        runner=runner or _default_runner(registry),
        sink=sink or _default_sink(),
        allowed_origins=allowed_origins
        or set(filter(None, os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(","))),
    )



def _default_sink():
    from gateway.sinks import default_sink
    return default_sink()


def _default_runner(registry: ToolRegistry):
    """Select the agent runtime: AGENT_RUNNER=mock (default) | adk | agent_engine."""
    kind = os.environ.get("AGENT_RUNNER", "mock")
    if kind == "adk":
        from gateway.adk_runner import AdkRunner
        return AdkRunner()
    if kind == "agent_engine":
        from gateway.adk_runner import AgentEngineRunner
        return AgentEngineRunner()
    return MockAgentRunner(registry=registry)


# -- request models -----------------------------------------------------------------
class SessionRequest(BaseModel):
    tenant: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class InteractRequest(BaseModel):
    action: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")   # e.g. slot_selected
    block_id: str
    payload: dict = Field(default_factory=dict)


class EventsRequest(BaseModel):
    events: list[dict] = Field(max_length=50)


def create_app(state: GatewayState | None = None) -> FastAPI:
    app = FastAPI(title="chat-gateway")
    st = state or build_state()

    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(st.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def check_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin and origin not in st.allowed_origins:
            raise HTTPException(403, "origin not allowed")

    def require_session(authorization: str = Header(default="")) -> dict:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        try:
            return st.tokens.verify_session(authorization.removeprefix("Bearer "))
        except TokenError as e:
            raise HTTPException(401, f"invalid session: {e}")

    def _tag(events: list[dict], claims: dict, turn_id: str) -> list[dict]:
        now = time.time()
        return [{**e, "tenant_id": claims["t"], "session_id": claims["sid"],
                 "turn_id": turn_id, "ts": now, "source": e.get("source", "agent")}
                for e in events]

    async def _stream_turn(claims: dict, message: str, context: dict):
        turn_id = f"turn_{uuid.uuid4().hex[:10]}"
        enabled = st.services.enabled_ui_blocks() + ["quick_replies"]
        async for raw in st.runner.run_turn(claims["sid"], message, context):
            clean, report = st.validator.validate(raw, enabled_blocks=enabled)
            st.sink.emit(_tag(clean.pop("events"), claims, turn_id))
            if report.dropped_blocks or report.degraded:
                st.sink.emit(_tag([{"name": "envelope_sanitized",
                                    "props": {"dropped": report.dropped_blocks,
                                              "degraded": report.degraded},
                                    "source": "gateway"}], claims, turn_id))
            yield f"data: {json.dumps({'turn_id': turn_id, **clean}, separators=(',', ':'))}\n\n"
        yield "data: [DONE]\n\n"

    # -- endpoints --------------------------------------------------------------
    @app.post("/v1/session")
    def create_session(body: SessionRequest, request: Request):
        check_origin(request)
        sid = f"s_{uuid.uuid4().hex[:16]}"
        return {"session_token": st.tokens.mint_session(body.tenant, sid), "session_id": sid}

    @app.get("/v1/config")
    def config(claims: dict = Depends(require_session)):
        return {
            "tenant": claims["t"],
            "block_registry_version": 1,
            "enabled_blocks": st.services.enabled_ui_blocks() + ["quick_replies"],
        }

    @app.post("/v1/chat")
    async def chat(body: ChatRequest, request: Request,
                   claims: dict = Depends(require_session)):
        check_origin(request)
        return StreamingResponse(_stream_turn(claims, body.message, {}),
                                 media_type="text/event-stream")

    @app.post("/v1/interact")
    async def interact(body: InteractRequest, request: Request,
                       claims: dict = Depends(require_session)):
        check_origin(request)
        # Human-tap rule: ONLY this endpoint mints interaction tokens, and only
        # because the widget reported a real user gesture on a rendered block.
        token = st.tokens.mint_interaction(claims["sid"], body.action, body.payload)
        st.tokens.verify_interaction(token, claims["sid"], body.action, body.payload)
        context = {"interaction": {"action": body.action, "block_id": body.block_id,
                                   "payload": body.payload, "interaction_token": token}}
        return StreamingResponse(
            _stream_turn(claims, f"[user interaction: {body.action}]", context),
            media_type="text/event-stream")

    @app.post("/v1/events")
    def ingest_events(body: EventsRequest, request: Request,
                      claims: dict = Depends(require_session)):
        check_origin(request)
        turn_id = "client"
        tagged = _tag([{**e, "source": "widget"} for e in body.events], claims, turn_id)
        st.sink.emit(tagged)
        return {"accepted": len(tagged)}

    app.state.gateway = st
    return app
