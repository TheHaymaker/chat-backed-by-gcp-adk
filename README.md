# web-chat-agent — Phases 0–3 (contracts, gateway, widget, knowledge, interactive flows)

Config-driven embeddable chat agent (Google ADK). This commit delivers the
contracts + tool registry + mock adapters vertical: the agent learns what
services are integrated from `services.yaml`, and everything runs mock-first
(no GCP, no real API required).

## What's here
- `contracts/` — envelope, UI block, and scheduling-kind JSON Schemas (the API
  between widget, gateway, and agent).
- `services.yaml` — drop-in service integration config (2 schedulers + 1 custom
  order-lookup service, all `mode: mock`).
- `agent/registry/` — config loader/validator, tool generation, prompt assembly,
  deterministic mock adapters (holds w/ TTL, interaction-token enforcement).
- `agent/app/app.py` — ADK App binding (FunctionTools from registry; Model Armor
  + BigQuery Agent Analytics plugin attach points behind env flags).
- `tools/validate_manifest.py` — CI gate for the config.
- `tests/` — 18 tests: validation, determinism, token rule, hold expiry, fixtures.

## Run
```bash
pip install pydantic pyyaml pytest
python -m pytest tests/ -q                      # 18 passed
python tools/validate_manifest.py services.yaml # config gate

# with ADK (playground):
pip install google-adk
adk web   # or: agents-cli playground
```

## Key invariants (tested)
1. Deterministic mocks: same service_id + date -> same slots (stable evals).
2. Human-tap rule: hold/confirm/cancel fail without an `itx_` interaction token,
   in mock mode too.
3. Live mode fails loudly until Phase 6 transports land.
4. Tools are namespaced `service_id.capability`; multiple services per kind coexist.

## Gateway (new)
- `gateway/security.py` — HMAC session tokens (`st_`) + interaction tokens (`itx_`)
  bound to (session, action, payload-hash) with TTL. Only `/v1/interact` mints them.
- `gateway/validation.py` — envelope enforcement against `contracts/`: invalid /
  unknown / tenant-disabled blocks dropped (with telemetry), text always survives.
- `gateway/runner.py` — AgentRunner seam (MockAgentRunner drives the real registry
  mocks) + EventSink seam (Memory/Log; Pub/Sub next).
- `gateway/main.py` — FastAPI: /v1/session, /v1/config, /v1/chat (SSE),
  /v1/interact, /v1/events.

Run it: `uvicorn gateway.main:create_app --factory --reload`

## Widget v0 (new)
- `widget/agent-widget.js` — single-file web component (Shadow DOM). Block
  registry renders text / quick_replies / scheduler / confirmation; unknown
  types degrade to text. HTML-escaped markdown with an https-only link
  whitelist. Theme via CSS custom properties (`--agent-accent`, etc.); system
  font stack; dark-mode + reduced-motion aware; keyboard focus visible.
- Event bus: `AgentWidget.on/use` with GA4 / dataLayer / webhook adapters;
  events batch to `/v1/events`.
- `widget/demo.html` — themed host page.

### Run the demo
```bash
uvicorn gateway.main:create_app --factory --port 8000   # terminal 1
python -m http.server 3000 -d widget                    # terminal 2
# open http://localhost:3000/demo.html -> "book a demo" -> tap a slot
```

## Phase 2: knowledge capabilities (new)
- Three new kinds: `faq`, `knowledge_base`, `site_search` — services declared in
  `services.yaml` (help-center, docs-kb, site-search) with fixture corpora.
- `agent/registry/mocks/knowledge.py` — deterministic lexical scoring; score
  floors implement the grounding rule (no weak matches -> empty result -> the
  agent says "I don't know" instead of fabricating a cited answer).
- Blocks: `faq_card` (expandable), `kb_answer` (mandatory citations — the
  gateway drops kb_answer blocks with empty citation lists), `search_results`.
- Live swap targets (Phase 6): FAQ store, Vertex AI RAG Engine, Vertex AI Search.

Try in the demo: "what's your refund policy?", "explain webhook signatures",
"find the pricing page", and (grounding rule) "how does medieval falconry work?"

## Phase 3 interactive flows (new)
- **form block** — typed fields, client-side required/email validation; submit
  routes through /v1/interact -> interaction token -> `sales-crm.capture_lead`
  (a WRITE tool: the CRM mock rejects tokenless calls, tested).
- **handoff block** — contact channels (mailto/tel/https-only urls).
- **quick_replies action mode** — options may carry `action` + `payload`; taps
  route through the token path instead of sending text. This is how cancel works.
- **cancel / reschedule intents** — per-session booking state; cancel is a
  token-gated tap; reschedule offers new slots and releases the old booking
  atomically on confirm (`booking_rescheduled` event carries from/to refs).
- New kind: `crm` (capture_lead, write) — service `sales-crm` in services.yaml.

Try in the demo: book a demo -> "reschedule that" -> pick a new time ->
"cancel my booking" -> tap Yes; then "talk to a human" and submit the form.

## Eval harness + deploy (new)
- `tests/eval/cases/*.yaml` — 18 declarative eval cases (routing, grounding,
  multi-turn flows, adversarial). `python tools/run_evals.py` executes them
  through the FULL gateway pipeline; 100% is a hard CI gate. The adversarial
  set proves the token invariant: no write effect ever occurs from text alone.
- `tools/export_adk_evalset.py` — emits ADK evalset JSON (tests/eval/adk/) for
  `agents-cli eval` / `adk eval` against the real agent (criteria in
  tests/eval/eval_config.json).
- `gateway/adk_runner.py` — AdkRunner (ADK api_server) + AgentEngineRunner;
  select via AGENT_RUNNER=mock|adk|agent_engine. Defensive envelope parsing;
  the gateway validator stays the enforcement layer.
- `agent/adk_app/` — the module `adk` CLI targets; instruction includes the
  interaction protocol (write tools only inside [interaction] turns, tokens
  passed verbatim).
- `Dockerfile.gateway` / `Dockerfile.agent`, `deploy/cloudbuild.yaml` (tests +
  evals gate image builds), `deploy/deploy-cloudrun.sh`, `deploy/README.md`
  (Cloud Run and Agent Engine paths, plugin enablement).

## GCP plumbing (new — Phases 4/5 in-repo halves)
- `agent/plugins/model_armor.py` — the full safety sandwich. `ArmorScreen`
  (pure decision layer, tested with fakes) + REST client + ADK plugin binding.
  Screens prompts (block -> canned refusal, zero inference cost), responses
  (block or SDP-redact), and tool/RAG output (indirect-injection defense).
  Fail policy: prompts fail CLOSED, tool output fails OPEN (override with
  MODEL_ARMOR_FAIL_OPEN). Verdicts flow to telemetry via on_verdict.
- `gateway/sinks.py` — PubSubSink (batched, fire-and-forget, can never break a
  turn) selected automatically when EVENTS_PUBSUB_TOPIC is set.
- `deploy/bigquery/` — `setup.sh` creates datasets, the product_events table
  (partitioned/clustered), the Pub/Sub -> BQ subscription, and the marts:
  `v_sessions` (product x agent_events join on session_id) and
  `v_booking_funnel` (conversion + tokens-per-booking per tenant/day).

GCP setup order: deploy/bigquery/setup.sh -> create a Model Armor template ->
deploy with ENABLE_MODEL_ARMOR=1 MODEL_ARMOR_TEMPLATE=<id>
ENABLE_BQ_ANALYTICS=1 EVENTS_PUBSUB_TOPIC=projects/<p>/topics/webchat-events.

## Live transports (new — Phase 6)
- `agent/registry/live/` — WebhookAdapter (HMAC-signed protocol: timestamp +
  v1 signature; reference verifier shipped for tenants), McpAdapter (JSON-RPC
  streamable HTTP with tool allow-lists), OpenApiAdapter (operationId mapping,
  path/query/body args). All share: per-service timeouts, 64KB output caps,
  circuit breakers (3 failures -> 30s cooldown -> half-open probe), env:// and
  secret-manager:// secret resolution, and the outbound token rule — write
  capabilities refuse to fire without a well-formed interaction token.
- `tests/contract/` — the Phase 6 gate: the scheduling contract asserted
  IDENTICALLY against the mock adapter and the live webhook path (our adapter
  -> HMAC wire -> reference tenant backend). Point it at a staging URL before
  flipping any service to prod-live.
- `registry.invoke()` — uniform call surface; the runner now uses it, so mock
  and live are interchangeable everywhere. Verified: all 18 evals pass with
  sales-scheduler flipped to `mode: live` against a real webhook server.

Flip a service: set `mode: live` + `live: {transport: webhook|mcp|openapi,
...}` in services.yaml. Nothing else changes.

## Placeholder services — confirm live tool calls actually work (new)
- `placeholders/backend.py` — a standalone multi-kind "tenant" backend (public
  HTTPS + HMAC) that generalizes `tests/contract/reference_backend.py` to every
  write/tool kind. It drives `ToolRegistry.invoke()` over the mock adapters, so
  it behaves exactly like the agent's mock path but over the real webhook wire.
- `services.live.yaml` — opt-in overlay flipping `sales-scheduler`,
  `support-calendar`, `sales-crm`, and `order-lookup` to `mode: live` (webhook),
  pointed at `${WEBHOOK_BASE_URL}/{service_id}` with `auth: env://WEBHOOK_SECRET`.
  Default `services.yaml` stays all-mock; use `SERVICES_CONFIG=services.live.yaml`.
- `python tools/smoke_live.py` — boots the backend on a real port and drives
  scheduling (get_availability→hold→confirm→cancel), CRM (`capture_lead`), and
  custom (`get_order_status`) over live HTTP, and asserts tokenless writes are
  refused. The headline proof that live tool calls work end to end.
- `docker compose up` — full local stack: `widget/demo.html` → gateway
  (`services.live.yaml`, MockAgentRunner) → live webhook → placeholder backend.

## Deploy to real GCP
- `deploy/RUNBOOK.md` — ordered, copy-pasteable: bootstrap → build/deploy → IAM
  grants → Model Armor / BigQuery → verify → teardown (region `us-central1`).
- `deploy/bootstrap.sh` — one-shot: enable APIs, create the Artifact Registry
  repo, optional analytics, then `deploy/deploy-cloudrun.sh`.
- `LIVE_SERVICES=1 ./deploy/deploy-cloudrun.sh` also deploys the placeholder
  backend and flips the four services to live — proving live tool calls against
  a real Cloud Run endpoint. Swap `WEBHOOK_BASE_URL` to a tenant's real webhook
  to go to production with no code change.

## Remaining (needs your GCP project / Phase 7)
- Evals against the live Gemini agent (ADK_BASE_URL=... --runner adk), then
  agents-cli eval for LLM quality; LB + Cloud Armor; dashboards; load/soak.
