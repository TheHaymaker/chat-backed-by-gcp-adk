#!/usr/bin/env bash
# Deploy web-chat-agent to Cloud Run: agent (ADK api_server) + gateway (BFF),
# and optionally the placeholder ("tenant") backend for live-mode services.
#
# Prereqs: gcloud auth, APIs enabled (run, artifactregistry, aiplatform,
# secretmanager, modelarmor, bigquery), and an Artifact Registry repo 'webchat'.
# deploy/bootstrap.sh does all of that for you first.
#
# Usage:
#   PROJECT=my-proj REGION=us-central1 ORIGINS=https://www.acme.com \
#     ./deploy/deploy-cloudrun.sh
#
#   # also stand up the placeholder backend and flip scheduling/CRM/orders to
#   # mode: live (proves live tool calls against a real deployed endpoint):
#   LIVE_SERVICES=1 PROJECT=... REGION=... ORIGINS=... ./deploy/deploy-cloudrun.sh
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
REGION="${REGION:-us-central1}"
ORIGINS="${ORIGINS:?set ORIGINS (comma-separated allowed origins)}"
TAG="${TAG:-$(date +%Y%m%d%H%M%S)}"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/webchat"
LIVE_SERVICES="${LIVE_SERVICES:-0}"

# Live mode flips the agent/gateway onto services.live.yaml and points it at the
# placeholder backend; default stays all-mock (no external endpoint needed).
SERVICES_FILE="services.yaml"
[ "$LIVE_SERVICES" = "1" ] && SERVICES_FILE="services.live.yaml"

echo "==> Gates: unit tests + structural evals + manifest"
python -m pytest tests/ -q
python tools/run_evals.py
python tools/validate_manifest.py services.yaml
[ "$LIVE_SERVICES" = "1" ] && python tools/validate_manifest.py services.live.yaml

echo "==> Secret: gateway signing key (created once)"
if ! gcloud secrets describe gateway-secret --project "$PROJECT" >/dev/null 2>&1; then
  openssl rand -hex 32 | gcloud secrets create gateway-secret \
    --project "$PROJECT" --data-file=-
fi

if [ "$LIVE_SERVICES" = "1" ]; then
  echo "==> Secret: webhook HMAC key shared by agent<->placeholder (created once)"
  if ! gcloud secrets describe webhook-secret --project "$PROJECT" >/dev/null 2>&1; then
    openssl rand -hex 32 | gcloud secrets create webhook-secret \
      --project "$PROJECT" --data-file=-
  fi
fi

echo "==> Build & push images (gateway, agent$([ "$LIVE_SERVICES" = "1" ] && echo ", placeholders"))"
gcloud builds submit --project "$PROJECT" --config deploy/cloudbuild.yaml \
  --substitutions=_REGION="$REGION",SHORT_SHA="$TAG" .

WEBHOOK_ENV=""
WEBHOOK_SECRETS=""
if [ "$LIVE_SERVICES" = "1" ]; then
  echo "==> Deploy placeholder backend (public HTTPS + HMAC — emulates a tenant webhook)"
  gcloud run deploy webchat-placeholders --project "$PROJECT" --region "$REGION" \
    --image "${REPO}/placeholders:${TAG}" \
    --allow-unauthenticated \
    --set-secrets "WEBHOOK_SECRET=webhook-secret:latest" \
    --set-env-vars "SERVICES_CONFIG=/app/services.yaml" \
    --memory 512Mi --cpu 1 --min-instances 0
  PLACEHOLDER_URL=$(gcloud run services describe webchat-placeholders --project "$PROJECT" \
    --region "$REGION" --format 'value(status.url)')
  WEBHOOK_ENV=",SERVICES_CONFIG=/app/${SERVICES_FILE},WEBHOOK_BASE_URL=${PLACEHOLDER_URL}"
  WEBHOOK_SECRETS="WEBHOOK_SECRET=webhook-secret:latest"
fi

echo "==> Deploy agent (internal ingress; only the gateway may call it)"
gcloud run deploy webchat-agent --project "$PROJECT" --region "$REGION" \
  --image "${REPO}/agent:${TAG}" \
  --no-allow-unauthenticated --ingress internal \
  ${WEBHOOK_SECRETS:+--set-secrets "$WEBHOOK_SECRETS"} \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},GOOGLE_GENAI_USE_VERTEXAI=1,ENABLE_MODEL_ARMOR=${ENABLE_MODEL_ARMOR:-0},ENABLE_BQ_ANALYTICS=${ENABLE_BQ_ANALYTICS:-0},BQ_ANALYTICS_DATASET_ID=agent_telemetry,MODEL_ARMOR_TEMPLATE=${MODEL_ARMOR_TEMPLATE:-}${WEBHOOK_ENV}" \
  --memory 1Gi --cpu 1 --min-instances 0
AGENT_URL=$(gcloud run services describe webchat-agent --project "$PROJECT" \
  --region "$REGION" --format 'value(status.url)')

echo "==> Deploy gateway (public, behind LB/Cloud Armor in prod)"
gcloud run deploy webchat-gateway --project "$PROJECT" --region "$REGION" \
  --image "${REPO}/gateway:${TAG}" \
  --allow-unauthenticated \
  --set-secrets "GATEWAY_SECRET=gateway-secret:latest${WEBHOOK_SECRETS:+,$WEBHOOK_SECRETS}" \
  --set-env-vars "ALLOWED_ORIGINS=${ORIGINS},AGENT_RUNNER=adk,ADK_BASE_URL=${AGENT_URL},ADK_APP_NAME=adk_app,EVENTS_PUBSUB_TOPIC=${EVENTS_PUBSUB_TOPIC:-}${WEBHOOK_ENV}" \
  --memory 512Mi --cpu 1 --min-instances 0
GATEWAY_URL=$(gcloud run services describe webchat-gateway --project "$PROJECT" \
  --region "$REGION" --format 'value(status.url)')

echo
echo "Done."
echo "  agent:        ${AGENT_URL} (internal)"
[ "$LIVE_SERVICES" = "1" ] && echo "  placeholders: ${PLACEHOLDER_URL} (live tenant webhook)"
echo "  gateway:      ${GATEWAY_URL}"
echo "  sample app:   widget/demo.html?endpoint=${GATEWAY_URL}"
echo "  embed:        <script src=\"agent-widget.js\" data-endpoint=\"${GATEWAY_URL}\" data-tenant=\"acme\"></script>"
echo
echo "Post-deploy checklist: give the gateway SA run.invoker on webchat-agent;"
echo "create BQ dataset 'agent_telemetry' + set ENABLE_BQ_ANALYTICS=1;"
echo "create a Model Armor template + set ENABLE_MODEL_ARMOR=1; front the"
echo "gateway with HTTPS LB + Cloud Armor rate limits. See deploy/RUNBOOK.md."
