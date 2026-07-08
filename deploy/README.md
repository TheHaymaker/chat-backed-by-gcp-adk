# Deploying web-chat-agent

Two runtime targets for the agent; the gateway is the same either way.

## Option A — Cloud Run (both services)

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com secretmanager.googleapis.com \
  modelarmor.googleapis.com bigquery.googleapis.com
gcloud artifacts repositories create webchat --repository-format=docker \
  --location=$REGION

PROJECT=my-proj REGION=us-central1 ORIGINS=https://www.acme.com \
  ./deploy/deploy-cloudrun.sh
```

Topology: `webchat-agent` runs `adk api_server` with **internal ingress** and
no unauthenticated access — grant the gateway's service account
`roles/run.invoker` on it. `webchat-gateway` is the only public surface; in
production put the HTTPS LB + Cloud Armor in front (rate limits, per-tenant
origin rules) and serve `widget/agent-widget.js` from your CDN.

## Option B — Agent Engine (managed agent runtime)

```bash
pip install google-adk
adk deploy agent_engine agent/adk_app \
  --project $PROJECT --region $REGION --display_name web-chat-agent
# or, with agents-cli scaffolding: agents-cli scaffold enhance --deployment-target agent_engine
```

Then deploy only the gateway container with:
`AGENT_RUNNER=agent_engine` and `AGENT_ENGINE=projects/.../reasoningEngines/...`.
Agent Engine gives you managed sessions, autoscaling, Agent Identity (grant the
agent's identity only its datastores/tools), and built-in OTel wiring.

## Turning on the plugins

- **BigQuery Agent Analytics**: create the dataset once
  (`bq mk --dataset $PROJECT:agent_telemetry`), grant the agent's SA
  `roles/bigquery.dataEditor` on it, set `ENABLE_BQ_ANALYTICS=1`. Events land
  in `agent_events`; enable the per-event-type views from the plugin docs.
- **Model Armor**: create a template (prompt-injection/jailbreak, malicious
  URI, SDP/PII filters) in the console or API, set `ENABLE_MODEL_ARMOR=1` and
  set `MODEL_ARMOR_TEMPLATE=<template-id>` — the plugin
  (`agent/plugins/model_armor.py`) screens prompts, responses, and tool output. Set project floor
  settings so tenant templates can't drop below org minimums.

## Eval workflow (CI gates)

1. `python -m pytest tests/ -q` — 50 unit/integration tests.
2. `python tools/run_evals.py` — 18 structural eval cases through the full
   gateway pipeline (schema validity, routing, grounding, token safety).
   **Must pass 100%**; deploy script and cloudbuild.yaml enforce this.
3. Against the real agent: `python tools/export_adk_evalset.py`, then
   `agents-cli eval` (or `adk eval agent/adk_app tests/eval/adk/*.evalset.json
   --config_file_path tests/eval/eval_config.json`) for the LLM-quality layer
   (tool trajectories, response match, judge criteria).
4. Same structural harness against the deployed agent:
   `ADK_BASE_URL=https://... python tools/run_evals.py --runner adk`.

## Swapping mock services for live

Per service in `services.yaml`: set `mode: live` + a `live:` transport
(webhook/mcp/openapi — Phase 6 adapters). Contract tests must pass against the
real endpoint in staging before flipping prod. Nothing else changes: same
tools, same prompt assembly, same blocks, same evals.

## Analytics pipeline

`PROJECT=my-proj ./deploy/bigquery/setup.sh` creates datasets, the
`product_events` table, the Pub/Sub topic + BigQuery subscription, and the
`v_sessions` / `v_booking_funnel` marts. Then set
`EVENTS_PUBSUB_TOPIC=projects/<p>/topics/webchat-events` on the gateway and
`ENABLE_BQ_ANALYTICS=1` on the agent. Point Looker Studio at the two views.
