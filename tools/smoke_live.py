#!/usr/bin/env python3
"""Live smoke proof: confirm scheduling / CRM / custom tool calls actually work
over the real webhook transport — not just as in-process mocks.

Boots placeholders/backend.py on a real localhost port, then drives every
live-flipped service in services.live.yaml through ToolRegistry.invoke() — the
exact path the agent uses (registry -> WebhookAdapter -> HTTP+HMAC -> tenant
backend). Asserts the happy paths succeed AND that a tokenless write is refused.

    python tools/smoke_live.py            # exits 0 on PASS, 1 on FAIL

No GCP, no network egress — everything runs against 127.0.0.1.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "smoke-secret-please-change"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_backend(port: int) -> "uvicorn.Server":
    import uvicorn
    from placeholders.backend import create_app

    app = create_app(services_path=ROOT / "services.yaml", secret=SECRET)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5).status_code == 200:
                return server
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("placeholder backend did not become ready")


def main() -> int:
    port = _free_port()
    os.environ["WEBHOOK_SECRET"] = SECRET
    os.environ["WEBHOOK_BASE_URL"] = f"http://127.0.0.1:{port}"

    server = _start_backend(port)

    # Import AFTER env is set so ${WEBHOOK_BASE_URL} expands in the live overlay.
    from agent.registry.config import load_services
    from agent.registry.live.base import InteractionTokenRequired
    from agent.registry.registry import ToolRegistry

    services = load_services(ROOT / "services.live.yaml")
    registry = ToolRegistry(services=services, base_dir=ROOT)
    itx = "itx_smoke"           # stands in for a gateway-minted interaction token
    passed: list[str] = []

    def ok(label: str) -> None:
        passed.append(label)
        print(f"  PASS  {label}")

    try:
        # 1. scheduling: full booking flow over the wire ----------------------
        avail = registry.invoke("sales-scheduler", "get_availability", date="2026-07-09")
        assert avail["slots"], "no slots returned"
        slot = avail["slots"][0]
        assert {"slot_id", "start", "end"} <= set(slot)
        ok("sales-scheduler.get_availability (live webhook)")

        hold = registry.invoke("sales-scheduler", "hold_slot",
                               slot_id=slot["slot_id"], interaction_token=itx)
        assert hold.get("hold_id")
        ok("sales-scheduler.hold_slot")

        booking = registry.invoke("sales-scheduler", "confirm_booking",
                                  hold_id=hold["hold_id"], interaction_token=itx)
        assert booking["start"] == slot["start"] and booking.get("booking_ref")
        ok("sales-scheduler.confirm_booking")

        cancelled = registry.invoke("sales-scheduler", "cancel_booking",
                                    booking_ref=booking["booking_ref"], interaction_token=itx)
        assert cancelled == {"cancelled": True}
        ok("sales-scheduler.cancel_booking")

        # 2. the human-tap rule holds over the live path ----------------------
        try:
            registry.invoke("sales-scheduler", "hold_slot", slot_id=slot["slot_id"])
            raise AssertionError("tokenless hold_slot should have been refused")
        except InteractionTokenRequired:
            ok("tokenless write refused (interaction-token rule)")

        # 3. crm: lead capture over the wire ----------------------------------
        lead = registry.invoke("sales-crm", "capture_lead",
                               values={"name": "Ada", "email": "ada@example.com",
                                       "message": "demo please"},
                               interaction_token=itx)
        assert lead.get("lead_ref")
        ok("sales-crm.capture_lead (live webhook)")

        # 4. custom: order lookup over the wire -------------------------------
        order = registry.invoke("order-lookup", "get_order_status", order_ref="A1001")
        assert order["status"] == "shipped"
        ok("order-lookup.get_order_status (live webhook)")

        # 5. second scheduler, distinct service -------------------------------
        avail2 = registry.invoke("support-calendar", "get_availability", date="2026-07-09")
        assert avail2["slots"]
        ok("support-calendar.get_availability (live webhook)")

    except Exception as e:  # noqa: BLE001 — smoke script: report and fail
        print(f"\nFAIL: {type(e).__name__}: {e}")
        server.should_exit = True
        return 1

    server.should_exit = True
    print(f"\n{len(passed)}/{len(passed)} live tool calls passed — "
          "scheduling, CRM and custom work over the real webhook transport.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
