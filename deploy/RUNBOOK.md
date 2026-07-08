# Deployment Runbook — web-chat-agent on Google Cloud

End-to-end, copy-pasteable steps to stand the system up on **real GCP** and test
it in a sample application. Region defaults to `us-central1`.

Two service topologies are covered:
- **Mock (default):** agent + gateway only; every integrated service runs its
  deterministic mock. Nothing external to stand up — good for a first deploy.
- **Live (`LIVE_SERVICES=1`):** additionally deploys the **placeholder backend**
  (a public HTTPS + HMAC endpoint that emulates a tenant's webhook) and flips
  `sales-scheduler`, `support-calendar`, `sales-crm`, and `order-lookup` to
  `mode: live`. This proves scheduling / CRM / custom tool calls execute over a
  real network hop against a deployed endpoint — not just in-process mocks.

---

## 0. Prerequisites (on your own machine — not the sandbox)

```bash
gcloud --version                       # Cloud SDK installed
gcloud auth login                      # interactive user creds, OR:
gcloud auth activate-service-account --key-file=KEY.json
gcloud config set project MY_PROJECT   # a billing-enabled project
docker --version                       # only needed for local runs (§7)
```

Requirements: a **billing-enabled** GCP project, `Owner` or an equivalent set of
admin roles (Run Admin, Artifact Registry Admin, Cloud Build Editor, Secret
Manager Admin, BigQuery Admin, Pub/Sub Admin, Service Account User), and Vertex
AI available in your region (`us-central1` is fully covered).

---

## 1. One-shot bootstrap (recommended)

```bash
# Mock-only first deploy:
PROJECT=MY_PROJECT REGION=us-central1 ORIGINS=https://www.acme.com \
  ./deploy/bootstrap.sh

# Or: live services + analytics in one go:
LIVE_SERVICES=1 ENABLE_BQ_ANALYTICS=1 \
  PROJECT=MY_PROJECT REGION=us-central1 ORIGINS=https://www.acme.com \
  ./deploy/bootstrap.sh
```

`bootstrap.sh` is idempotent and runs, in order: enable APIs → create the
`webchat` Artifact Registry repo → (optional) BigQuery/Pub-Sub analytics →
`deploy-cloudrun.sh` (which gates on tests + evals before building images).

`ORIGINS` is the comma-separated allowlist of host pages permitted to embed the
widget (CORS + origin check). Use your real site origin(s); for the sample app
served locally add `http://localhost:3000`.

If you prefer to run the phases yourself, do §2–§6 individually.

---

## 2. APIs, registry, secrets (what bootstrap does)

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com aiplatform.googleapis.com \
  secretmanager.googleapis.com modelarmor.googleapis.com \
  bigquery.googleapis.com pubsub.googleapis.com

gcloud artifacts repositories create webchat \
  --repository-format=docker --location=us-central1
```

Secrets are created automatically by `deploy-cloudrun.sh`:
- `gateway-secret` — HMAC key for session/interaction tokens (`st_`/`itx_`).
- `webhook-secret` — HMAC key shared between the agent and the placeholder
  backend (live mode only).

---

## 3. Build + deploy (what deploy-cloudrun.sh does)

Gates first (`pytest`, `run_evals.py` must be 100%, `validate_manifest`), then
`gcloud builds submit --config deploy/cloudbuild.yaml` builds and pushes the
`gateway`, `agent`, and `placeholders` images. Then:

| Service | Ingress | Auth | Notes |
|---|---|---|---|
| `webchat-agent` | internal | authenticated | ADK `api_server`; only the gateway may call it |
| `webchat-gateway` | public | unauth (front with LB/Cloud Armor) | the only public surface |
| `webchat-placeholders` | public | unauth + **HMAC** | live mode only; emulates a tenant webhook |

The placeholder is intentionally public + HMAC-authenticated because that is
exactly how a real tenant webhook works (see the protocol in
`agent/registry/live/webhook.py`). Swap `WEBHOOK_BASE_URL` to a tenant's real
endpoint to go from placeholder to production with no code change.

---

## 4. IAM grants (run once, after first deploy)

```bash
PROJECT=MY_PROJECT REGION=us-central1
GATEWAY_SA=$(gcloud run services describe webchat-gateway --region "$REGION" \
  --format 'value(spec.template.spec.serviceAccountName)')
AGENT_SA=$(gcloud run services describe webchat-agent --region "$REGION" \
  --format 'value(spec.template.spec.serviceAccountName)')

# Gateway may invoke the internal agent:
gcloud run services add-iam-policy-binding webchat-agent --region "$REGION" \
  --member "serviceAccount:${GATEWAY_SA}" --role roles/run.invoker

# Agent identity: Vertex, its telemetry dataset, and the webhook secret:
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${AGENT_SA}" --role roles/aiplatform.user
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${AGENT_SA}" --role roles/secretmanager.secretAccessor
# BigQuery analytics (if ENABLE_BQ_ANALYTICS=1):
bq add-iam-policy-binding \
  --member "serviceAccount:${AGENT_SA}" --role roles/bigquery.dataEditor \
  "${PROJECT}:agent_telemetry" 2>/dev/null || \
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${AGENT_SA}" --role roles/bigquery.dataEditor
```

The placeholder authenticates callers by HMAC, not IAM, so no invoker grant is
needed for agent→placeholder traffic.

---

## 5. Optional hardening / analytics

**Model Armor** (safety sandwich — `agent/plugins/model_armor.py`):
```bash
# Create a template (prompt-injection/jailbreak, malicious-URI, SDP/PII, RAI)
# in the console or API, then redeploy the agent with it enabled:
ENABLE_MODEL_ARMOR=1 MODEL_ARMOR_TEMPLATE=projects/MY_PROJECT/locations/us-central1/templates/webchat \
  LIVE_SERVICES=1 PROJECT=MY_PROJECT ORIGINS=... ./deploy/deploy-cloudrun.sh
```
Set project **floor settings** so tenant templates can't drop below the org
minimum. Prompts fail CLOSED; tool output fails OPEN (`MODEL_ARMOR_FAIL_OPEN`).

**BigQuery + Pub/Sub analytics** (`deploy/bigquery/`):
```bash
PROJECT=MY_PROJECT ./deploy/bigquery/setup.sh    # datasets, product_events,
                                                 # Pub/Sub->BQ sub, marts
# then redeploy the gateway with:
EVENTS_PUBSUB_TOPIC=projects/MY_PROJECT/topics/webchat-events
# and the agent with ENABLE_BQ_ANALYTICS=1.
```
Point Looker Studio at `v_sessions` and `v_booking_funnel`.

**Edge:** put an HTTPS Load Balancer + Cloud Armor in front of the gateway for
per-tenant rate limits, bot rules, and origin enforcement; serve
`widget/agent-widget.js` from your CDN.

---

## 6. Verify the deployment

```bash
GATEWAY_URL=$(gcloud run services describe webchat-gateway --region us-central1 \
  --format 'value(status.url)')

# 6a. gateway is up + mints a session:
curl -s -X POST "$GATEWAY_URL/v1/session" \
  -H 'Origin: https://www.acme.com' -H 'Content-Type: application/json' \
  -d '{"tenant":"acme"}'

# 6b. structural evals against the deployed agent (schema/routing/grounding/tokens):
ADK_BASE_URL=$(gcloud run services describe webchat-agent --region us-central1 \
  --format 'value(status.url)') python tools/run_evals.py --runner adk

# 6c. LLM-quality evals against the real Gemini agent:
python tools/export_adk_evalset.py
adk eval agent/adk_app tests/eval/adk/*.evalset.json \
  --config_file_path tests/eval/eval_config.json
```

**Sample application:** open
`widget/demo.html?endpoint=<GATEWAY_URL>` (serve the `widget/` folder from any
static host, or your CDN). Type *"book a demo"* and tap a slot — in live mode the
booking is executed against `webchat-placeholders` over the HMAC webhook.

---

## 7. Local dry-run before touching GCP (no gcloud needed)

```bash
# Prove live scheduling/CRM/custom tool calls over the real webhook transport:
python tools/smoke_live.py

# Full local stack (widget -> gateway -> live webhook -> placeholder):
WEBHOOK_SECRET=$(openssl rand -hex 32) docker compose up --build
# open http://localhost:3000/demo.html  ->  "book a demo"  ->  tap a slot
```

---

## 9. Deploy from your phone (Cloud Shell)

You don't need a laptop. The **Google Cloud app** now includes a built-in Cloud
Shell (and Gemini Cloud Assist can open it to run `gcloud` for you); you can also
open `shell.cloud.google.com` in mobile Chrome/Safari. Cloud Shell is
auto-authenticated as you and ships with `gcloud`, `git`, and `docker`.

1. Open **Cloud Shell** (Google Cloud app → Cloud Shell, or the URL above).
2. Run:
   ```bash
   git clone https://github.com/TheHaymaker/chat-backed-by-gcp-adk
   cd chat-backed-by-gcp-adk && git checkout claude/new-session-5ozuhr
   export PROJECT=MY_PROJECT REGION=us-central1 ORIGINS=https://www.acme.com
   ./deploy/bootstrap.sh
   # live services + analytics instead:
   # LIVE_SERVICES=1 ENABLE_BQ_ANALYTICS=1 ./deploy/bootstrap.sh
   ```
   Set the three vars once with `export` so you're not retyping them — that's the
   only real friction on a phone keyboard.
3. The image builds run **server-side in Cloud Build** (`gcloud builds submit`),
   so your phone needs no horsepower and the ephemeral Cloud Shell VM is fine.

**Monitor + roll back from the app UI.** Use the Google Cloud app's tap-around
UI (which can't run the multi-service deploy itself) to watch Cloud Run service
status, Cloud Build history, and logs, and to roll back to a previous revision.

**Verify from the same Cloud Shell:**
```bash
GATEWAY_URL=$(gcloud run services describe webchat-gateway --region "$REGION" \
  --format 'value(status.url)')
curl -s -X POST "$GATEWAY_URL/v1/session" \
  -H "Origin: $ORIGINS" -H 'Content-Type: application/json' -d '{"tenant":"acme"}'
```
Then open `widget/demo.html?endpoint=$GATEWAY_URL` in your phone browser (serve
`widget/` from any static host / CDN) and try *"book a demo"*.

---

## 10. Teardown

```bash
PROJECT=MY_PROJECT REGION=us-central1
for s in webchat-gateway webchat-agent webchat-placeholders; do
  gcloud run services delete "$s" --region "$REGION" --quiet 2>/dev/null || true
done
gcloud artifacts repositories delete webchat --location "$REGION" --quiet
gcloud secrets delete gateway-secret --quiet 2>/dev/null || true
gcloud secrets delete webhook-secret --quiet 2>/dev/null || true
# analytics (optional): drop datasets + Pub/Sub
bq rm -r -f "${PROJECT}:webchat_analytics" 2>/dev/null || true
bq rm -r -f "${PROJECT}:agent_telemetry" 2>/dev/null || true
gcloud pubsub subscriptions delete webchat-events-to-bq --quiet 2>/dev/null || true
gcloud pubsub topics delete webchat-events --quiet 2>/dev/null || true
```
