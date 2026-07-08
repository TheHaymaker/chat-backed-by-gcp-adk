"""AdkRunner — points the gateway at the real ADK agent.

Two targets, selected by env:
  ADK_BASE_URL   -> ADK api_server / Cloud Run  (adk api_server agent/)
  AGENT_ENGINE   -> Vertex AI Agent Engine resource name (uses google-auth)

Protocol with the agent (matches agent/adk_app/agent.py instructions):
  - Plain user turns are sent as-is.
  - UI interactions are sent as a structured line the instruction defines:
      [interaction] {"action": ..., "block_id": ..., "payload": ..., "interaction_token": "itx_..."}
    The agent must pass interaction_token through to write tools verbatim;
    tools reject anything else, and the gateway signature is the real check.
  - The agent replies with the JSON envelope (possibly fenced); we parse
    defensively — the gateway validator remains the enforcement layer.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field

import httpx

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_envelope(text: str) -> dict:
    """Extract the envelope from model text. Fallback: treat text as message."""
    candidate = _FENCE.sub("", text.strip())
    try:
        data = json.loads(candidate)
        if isinstance(data, dict) and "message" in data:
            data.setdefault("ui_blocks", [])
            data.setdefault("events", [])
            return data
    except json.JSONDecodeError:
        # try the largest {...} span (models sometimes add prose around JSON)
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                data = json.loads(candidate[start:end + 1])
                if isinstance(data, dict) and "message" in data:
                    data.setdefault("ui_blocks", [])
                    data.setdefault("events", [])
                    return data
            except json.JSONDecodeError:
                pass
    return {"message": text.strip(), "ui_blocks": [], "events": []}


@dataclass
class AdkRunner:
    base_url: str = field(default_factory=lambda: os.environ.get(
        "ADK_BASE_URL", "http://localhost:8080"))
    app_name: str = field(default_factory=lambda: os.environ.get(
        "ADK_APP_NAME", "adk_app"))
    timeout_s: float = 60.0
    _sessions: set = field(default_factory=set)

    def _msg(self, user_message: str, context: dict) -> str:
        interaction = context.get("interaction")
        if interaction:
            return "[interaction] " + json.dumps(interaction, separators=(",", ":"))
        return user_message

    async def _ensure_session(self, client: httpx.AsyncClient, session_id: str) -> None:
        if session_id in self._sessions:
            return
        r = await client.post(
            f"{self.base_url}/apps/{self.app_name}/users/widget/sessions/{session_id}",
            json={})
        if r.status_code not in (200, 400):     # 400: already exists
            r.raise_for_status()
        self._sessions.add(session_id)

    async def run_turn(self, session_id: str, user_message: str, context: dict):
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            await self._ensure_session(client, session_id)
            r = await client.post(f"{self.base_url}/run", json={
                "app_name": self.app_name,
                "user_id": "widget",
                "session_id": session_id,
                "new_message": {
                    "role": "user",
                    "parts": [{"text": self._msg(user_message, context)}],
                },
            })
            r.raise_for_status()
            events = r.json()
            # Final response = last event carrying model text parts.
            text = ""
            for ev in events:
                for part in (ev.get("content") or {}).get("parts", []):
                    if part.get("text"):
                        text = part["text"]
            yield parse_envelope(text or "")


@dataclass
class AgentEngineRunner:
    """Vertex AI Agent Engine target (deployed via `adk deploy agent_engine`
    or `agents-cli scaffold enhance --deployment-target agent_engine`)."""
    resource_name: str = field(default_factory=lambda: os.environ["AGENT_ENGINE"])
    location: str = field(default_factory=lambda: os.environ.get(
        "GOOGLE_CLOUD_LOCATION", "us-central1"))
    timeout_s: float = 60.0

    def _token(self) -> str:
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token

    async def run_turn(self, session_id: str, user_message: str, context: dict):
        interaction = context.get("interaction")
        msg = ("[interaction] " + json.dumps(interaction, separators=(",", ":"))
               if interaction else user_message)
        url = (f"https://{self.location}-aiplatform.googleapis.com/v1/"
               f"{self.resource_name}:streamQuery")
        text = ""
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.post(url,
                headers={"Authorization": f"Bearer {self._token()}"},
                json={"input": {"message": msg, "session_id": session_id,
                                "user_id": "widget"}})
            r.raise_for_status()
            for line in r.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for part in (ev.get("content") or {}).get("parts", []):
                    if part.get("text"):
                        text = part["text"]
        yield parse_envelope(text or "")
