"""Agent runner abstraction and event sinks.

AgentRunner is the seam between the gateway and the agent runtime:
  - MockAgentRunner: in-process, drives the SAME ToolRegistry mocks the real
    agent uses — so gateway tests exercise real scheduler payloads.
  - AdkRunner (Phase 1 deploy): calls Agent Engine / ADK api_server.

EventSink is the seam for product analytics:
  - MemorySink (tests), LogSink (dev). PubSubSink lands with GCP wiring.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from agent.registry.registry import ToolRegistry

logger = logging.getLogger("gateway.events")


# -- runners --------------------------------------------------------------------
class AgentRunner(Protocol):
    async def run_turn(self, session_id: str, user_message: str,
                       context: dict) -> AsyncIterator[dict]:
        """Yield one or more raw envelope dicts (pre-validation)."""
        ...


@dataclass
class MockAgentRunner:
    """Deterministic scripted runner for dev/tests.

    Mirrors the routing policy the real ADK agent's prompt will encode:
    booking / cancel / reschedule intents, form + handoff for contact,
    site search for navigation, FAQ-first, grounded KB, admit ignorance.
    Per-session state tracks the last booking to make cancel/reschedule real.
    """
    registry: ToolRegistry
    scheduler_id: str = "sales-scheduler"
    crm_id: str = "sales-crm"
    _state: dict = field(default_factory=dict)   # session_id -> {last_booking, reschedule_of}

    def _sched_block(self, note: str) -> dict:
        slots = self.registry.invoke(self.scheduler_id, "get_availability",
                                     date="2026-07-09")["slots"][:4]
        return {
            "message": note,
            "ui_blocks": [{
                "type": "scheduler", "id": "blk_sched1",
                "props": {"service_id": self.scheduler_id, "timezone": "UTC",
                          "slots": slots},
            }],
            "events": [{"name": "scheduler_offered",
                        "props": {"service_id": self.scheduler_id}}],
        }

    async def run_turn(self, session_id: str, user_message: str, context: dict):
        def sched(cap, **kw):
            return self.registry.invoke(self.scheduler_id, cap, **kw)
        st = self._state.setdefault(session_id, {})
        interaction = context.get("interaction")

        if interaction:
            action = interaction.get("action")
            token = interaction["interaction_token"]
            payload = interaction.get("payload", {})

            if action == "slot_selected":
                hold = sched("hold_slot", slot_id=payload["slot_id"],
                             interaction_token=token)
                booking = sched("confirm_booking", hold_id=hold["hold_id"],
                                interaction_token=token)
                events = [{"name": "booking_completed",
                           "props": {"service_id": self.scheduler_id}}]
                msg = "You're booked! A confirmation is below."
                old = st.pop("reschedule_of", None)
                if old:
                    sched("cancel_booking", booking_ref=old,
                          interaction_token=token)
                    events.append({"name": "booking_rescheduled",
                                   "props": {"from": old,
                                             "to": booking["booking_ref"]}})
                    msg = "Rescheduled — your old time is released. New details below."
                st["last_booking"] = booking
                yield {
                    "message": msg,
                    "ui_blocks": [{
                        "type": "confirmation", "id": "blk_conf1",
                        "props": {"service_id": self.scheduler_id,
                                  "booking_ref": booking["booking_ref"],
                                  "start": booking["start"], "end": booking["end"],
                                  "summary": "Demo call"},
                    }],
                    "events": events,
                }
                return

            if action == "cancel_booking":
                sched("cancel_booking", booking_ref=payload["booking_ref"],
                      interaction_token=token)
                st.pop("last_booking", None)
                yield {
                    "message": "Done — your booking is cancelled. "
                               "Want to pick a new time instead?",
                    "ui_blocks": [{
                        "type": "quick_replies", "id": "blk_qr_rebook",
                        "props": {"options": [{"label": "Book a new time",
                                               "value": "book a demo"}]},
                    }],
                    "events": [{"name": "booking_cancelled",
                                "props": {"booking_ref": payload["booking_ref"]}}],
                }
                return

            if action == "form_submitted":
                lead = self.registry.invoke(self.crm_id, "capture_lead",
                                            values=payload.get("values", {}),
                                            interaction_token=token)
                yield {
                    "message": "Thanks — the team has your details and will reach "
                               f"out shortly (ref {lead['lead_ref']}).",
                    "ui_blocks": [],
                    "events": [{"name": "lead_captured",
                                "props": {"lead_ref": lead["lead_ref"],
                                          "form_id": payload.get("form_id")}}],
                }
                return

            yield {"message": "I couldn't process that interaction.",
                   "ui_blocks": [], "events": [{"name": "interaction_unhandled",
                                                "props": {"action": action}}]}
            return

        lowered = user_message.lower()

        # Cancel / reschedule intents (write ops -> route the tap through /v1/interact)
        if "cancel" in lowered or "reschedule" in lowered:
            booking = st.get("last_booking")
            if not booking:
                yield {"message": "I don't see an active booking on this session. "
                                  "Want to book a time?",
                       "ui_blocks": [{"type": "quick_replies", "id": "blk_qr_b",
                                      "props": {"options": [{"label": "Book a demo",
                                                             "value": "book a demo"}]}}],
                       "events": []}
                return
            if "reschedule" in lowered:
                st["reschedule_of"] = booking["booking_ref"]
                yield self._sched_block(
                    "Sure — pick a new time and I'll release the old one:")
                return
            yield {
                "message": f"You have a booking ({booking['booking_ref']}). "
                           "Cancel it?",
                "ui_blocks": [{
                    "type": "quick_replies", "id": "blk_qr_cancel",
                    "props": {"options": [
                        {"label": "Yes, cancel it", "value": "confirm cancel",
                         "action": "cancel_booking",
                         "payload": {"booking_ref": booking["booking_ref"]}},
                        {"label": "Keep it", "value": "keep my booking"},
                    ]},
                }],
                "events": [{"name": "cancel_offered", "props": {}}],
            }
            return

        # Contact / lead capture -> form + handoff in one envelope
        if any(w in lowered for w in ("contact", "human", "support", "get in touch")):
            yield {
                "message": "Leave your details and the team will reach out — "
                           "or grab us directly:",
                "ui_blocks": [
                    {"type": "form", "id": "blk_lead1",
                     "props": {"form_id": "lead_capture",
                               "title": "Contact the team",
                               "submit_label": "Send",
                               "fields": [
                                   {"name": "name", "label": "Name",
                                    "type": "text", "required": True},
                                   {"name": "email", "label": "Work email",
                                    "type": "email", "required": True},
                                   {"name": "message", "label": "How can we help?",
                                    "type": "textarea", "required": False},
                               ]}},
                    {"type": "handoff", "id": "blk_ho1",
                     "props": {"reason": "human requested",
                               "channels": [
                                   {"kind": "email", "label": "Email support",
                                    "value": "support@acme.example"},
                                   {"kind": "url", "label": "Help center",
                                    "value": "https://acme.example/help"},
                               ]}},
                ],
                "events": [{"name": "handoff_requested", "props": {}}],
            }
            return

        if any(w in lowered for w in ("book", "demo", "schedule")):
            yield self._sched_block("Here are some times that work — tap one to book:")
            return

        # Navigational intent -> site search
        if any(w in lowered for w in ("find", "where is", "page", "link")):
            hits = self.registry.invoke("site-search", "search",
                                        query=user_message)["results"]
            if hits:
                yield {
                    "message": "Here's what I found on the site:",
                    "ui_blocks": [{
                        "type": "search_results", "id": "blk_sr1",
                        "props": {"query": user_message[:200],
                                  "results": [{"title": h["title"],
                                               "snippet": h.get("snippet", ""),
                                               "url": h["url"]} for h in hits]},
                    }],
                    "events": [{"name": "search_performed",
                                "props": {"hits": len(hits)}}],
                }
                return

        # FAQ-first: high-confidence curated answers skip retrieval
        faq = self.registry.invoke("help-center", "lookup",
                                   query=user_message)["matches"]
        if faq:
            top = faq[0]
            yield {
                "message": "This should cover it:",
                "ui_blocks": [{
                    "type": "faq_card", "id": "blk_faq1",
                    "props": {k: v for k, v in {
                        "question": top["question"],
                        "answer_markdown": top["answer_markdown"],
                        "url": top.get("url")}.items() if v is not None},
                }],
                "events": [{"name": "faq_answered",
                            "props": {"score": top["score"]}}],
            }
            return

        # Knowledge base: answer ONLY from retrieved chunks, always cited
        chunks = self.registry.invoke("docs-kb", "retrieve",
                                      query=user_message)["chunks"]
        if chunks:
            yield {
                "message": "From the docs:",
                "ui_blocks": [{
                    "type": "kb_answer", "id": "blk_kb1",
                    "props": {"markdown": chunks[0]["text"],
                              "citations": [{"title": c["title"], "url": c["url"]}
                                            for c in chunks]},
                }],
                "events": [{"name": "kb_answered",
                            "props": {"chunks": len(chunks)}}],
            }
            return
        if any(w in lowered for w in ("how", "what", "why", "does", "explain")):
            # Grounding rule: no chunks above the floor -> say so, never fabricate
            yield {
                "message": "I couldn't find that in the documentation, so I won't guess. "
                           "Want me to connect you with the team?",
                "ui_blocks": [{
                    "type": "quick_replies", "id": "blk_qr_esc",
                    "props": {"options": [{"label": "Contact support",
                                           "value": "I want to contact support"}]},
                }],
                "events": [{"name": "kb_no_answer", "props": {}}],
            }
            return

        yield {
            "message": f"You said: {user_message}",
            "ui_blocks": [{
                "type": "quick_replies", "id": "blk_qr1",
                "props": {"options": [{"label": "Book a demo", "value": "book a demo"}]},
            }],
            "events": [],
        }


# -- event sinks -------------------------------------------------------------------
class EventSink(Protocol):
    def emit(self, events: list[dict]) -> None: ...


@dataclass
class MemorySink:
    events: list[dict] = field(default_factory=list)

    def emit(self, events: list[dict]) -> None:
        self.events.extend(events)


class LogSink:
    def emit(self, events: list[dict]) -> None:
        for e in events:
            logger.info("event %s", json.dumps(e, separators=(",", ":")))
