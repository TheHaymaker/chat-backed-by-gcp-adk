"""Model Armor plugin — the safety sandwich.

Three screening points, per the architecture:
  before_model  -> sanitizeUserPrompt   (block turn before any inference)
  after_model   -> sanitizeModelResponse (replace/redact before the gateway)
  after_tool    -> screen tool/RAG output (indirect prompt-injection defense)

Layered so it's testable without GCP:
  ArmorScreen        pure decision logic against a client protocol
  ModelArmorClient   real REST client (httpx + google-auth), lazy imports
  build_model_armor_plugin()  binds ArmorScreen into an ADK BasePlugin
                     (imported only when ENABLE_MODEL_ARMOR=1; pin your ADK
                     version — plugin callback signatures still evolve)

Env: MODEL_ARMOR_TEMPLATE (template id), GOOGLE_CLOUD_PROJECT,
GOOGLE_CLOUD_LOCATION, MODEL_ARMOR_FAIL_OPEN (default closed for prompts,
open for tool output — see notes inline).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

logger = logging.getLogger("agent.model_armor")

BLOCKED_PROMPT_MESSAGE = (
    "I can't help with that request. If you think this was blocked in error, "
    "try rephrasing, or use the contact option to reach the team.")
BLOCKED_RESPONSE_MESSAGE = (
    "I generated a response that didn't pass our safety checks, so I've "
    "withheld it. Could you rephrase what you're looking for?")
BLOCKED_TOOL_NOTE = "[content removed by safety screening]"


class ArmorClient(Protocol):
    def sanitize_user_prompt(self, text: str) -> dict: ...
    def sanitize_model_response(self, text: str) -> dict: ...


@dataclass
class Verdict:
    blocked: bool
    redacted_text: Optional[str] = None   # SDP de-identified text, if provided
    filters: list[str] = field(default_factory=list)


def parse_result(raw: dict) -> Verdict:
    """Normalize a Model Armor sanitization result."""
    result = raw.get("sanitizationResult", raw)
    state = result.get("filterMatchState", "NO_MATCH_FOUND")
    filters = [name for name, fr in (result.get("filterResults") or {}).items()
               if isinstance(fr, dict) and json.dumps(fr).find("MATCH_FOUND") >= 0]
    redacted = None
    sdp = (result.get("filterResults") or {}).get("sdp") or {}
    deident = (sdp.get("sdpFilterResult") or {}).get("deidentifyResult") or {}
    if deident.get("data", {}).get("text"):
        redacted = deident["data"]["text"]
    return Verdict(blocked=state == "MATCH_FOUND", redacted_text=redacted,
                   filters=filters)


@dataclass
class ArmorScreen:
    """Decision layer. fail_open: what to do when the Armor API itself errors.
    Default policy: prompts fail CLOSED (an unscreened prompt is the riskiest
    input), tool output fails OPEN with logging (availability over strictness
    for read paths) — override via MODEL_ARMOR_FAIL_OPEN=prompt,response,tool.
    """
    client: ArmorClient
    fail_open: set = field(default_factory=lambda: {
        s.strip() for s in os.environ.get("MODEL_ARMOR_FAIL_OPEN", "tool").split(",")})
    on_verdict: Any = None    # callable(stage, verdict) for telemetry

    def _screen(self, stage: str, text: str, respond: bool) -> Verdict:
        try:
            raw = (self.client.sanitize_model_response(text) if respond
                   else self.client.sanitize_user_prompt(text))
            verdict = parse_result(raw)
        except Exception as e:  # Armor API failure
            logger.error("model armor %s screening failed: %s", stage, e)
            verdict = Verdict(blocked=stage not in self.fail_open,
                              filters=["screening_error"])
        if self.on_verdict:
            self.on_verdict(stage, verdict)
        return verdict

    # -- the three screening points -----------------------------------------
    def screen_prompt(self, text: str) -> Optional[str]:
        """Returns a canned refusal to short-circuit with, or None to proceed."""
        v = self._screen("prompt", text, respond=False)
        return BLOCKED_PROMPT_MESSAGE if v.blocked else None

    def screen_response(self, text: str) -> Optional[str]:
        """Returns replacement text (canned or SDP-redacted), or None to pass."""
        v = self._screen("response", text, respond=True)
        if v.blocked:
            return v.redacted_text or BLOCKED_RESPONSE_MESSAGE
        return v.redacted_text  # partial redaction may apply without a block

    def screen_tool_output(self, result: Any) -> Any:
        """Tool/RAG output is untrusted input; screen it as a user prompt."""
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        v = self._screen("tool", text[:20_000], respond=False)
        return BLOCKED_TOOL_NOTE if v.blocked else result


class ModelArmorClient:
    """REST client for the Model Armor regional endpoint."""

    def __init__(self) -> None:
        self.project = os.environ["GOOGLE_CLOUD_PROJECT"]
        self.location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        self.template = os.environ["MODEL_ARMOR_TEMPLATE"]
        self._base = (f"https://modelarmor.{self.location}.rep.googleapis.com/v1/"
                      f"projects/{self.project}/locations/{self.location}/"
                      f"templates/{self.template}")

    def _post(self, verb: str, body: dict) -> dict:
        import google.auth
        import google.auth.transport.requests
        import httpx
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        r = httpx.post(f"{self._base}:{verb}", json=body, timeout=10.0,
                       headers={"Authorization": f"Bearer {creds.token}"})
        r.raise_for_status()
        return r.json()

    def sanitize_user_prompt(self, text: str) -> dict:
        return self._post("sanitizeUserPrompt", {"userPromptData": {"text": text}})

    def sanitize_model_response(self, text: str) -> dict:
        return self._post("sanitizeModelResponse", {"modelResponseData": {"text": text}})


def build_model_armor_plugin():
    """Bind ArmorScreen into an ADK plugin. Lazy ADK import (env-gated)."""
    from google.adk.plugins.base_plugin import BasePlugin
    from google.genai import types

    screen = ArmorScreen(client=ModelArmorClient())

    class ModelArmorPlugin(BasePlugin):
        def __init__(self) -> None:
            super().__init__(name="model_armor")

        async def before_model_callback(self, *, callback_context, llm_request):
            texts = []
            for content in getattr(llm_request, "contents", []) or []:
                if getattr(content, "role", "") == "user":
                    texts += [p.text for p in (content.parts or []) if getattr(p, "text", None)]
            if not texts:
                return None
            refusal = screen.screen_prompt("\n".join(texts[-3:]))
            if refusal:
                from google.adk.models.llm_response import LlmResponse
                envelope = json.dumps({"message": refusal, "ui_blocks": [],
                                       "events": [{"name": "armor_blocked_prompt",
                                                   "props": {}}]})
                return LlmResponse(content=types.Content(
                    role="model", parts=[types.Part(text=envelope)]))
            return None

        async def after_model_callback(self, *, callback_context, llm_response):
            parts = (getattr(llm_response, "content", None) or
                     types.Content(parts=[])).parts or []
            text = "".join(p.text for p in parts if getattr(p, "text", None))
            if not text:
                return None
            replacement = screen.screen_response(text)
            if replacement is None:
                return None
            envelope = json.dumps({"message": replacement, "ui_blocks": [],
                                   "events": [{"name": "armor_screened_response",
                                               "props": {}}]})
            from google.adk.models.llm_response import LlmResponse
            return LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text=envelope)]))

        async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
            screened = screen.screen_tool_output(result)
            return screened if screened is not result else None

    return ModelArmorPlugin()
