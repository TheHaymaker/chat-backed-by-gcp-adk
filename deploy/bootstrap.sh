#!/usr/bin/env bash
# One-shot bootstrap: everything a fresh GCP project needs, in order, then deploy.
# Idempotent — safe to re-run. See deploy/RUNBOOK.md for the annotated walkthrough.
#
# Usage:
#   PROJECT=my-proj REGION=us-central1 ORIGINS=https://www.acme.com \
#     ./deploy/bootstrap.sh
#
#   # include the placeholder backend + live-mode scheduling/CRM/orders:
#   LIVE_SERVICES=1 ENABLE_BQ_ANALYTICS=1 PROJECT=... ORIGINS=... ./deploy/bootstrap.sh
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
REGION="${REGION:-us-central1}"
ORIGINS="${ORIGINS:?set ORIGINS (comma-separated allowed origins)}"
export PROJECT REGION ORIGINS

echo "==> 1/4 Enable APIs"
gcloud services enable --project "$PROJECT" \
  run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  aiplatform.googleapis.com secretmanager.googleapis.com \
  modelarmor.googleapis.com bigquery.googleapis.com pubsub.googleapis.com

echo "==> 2/4 Artifact Registry repo 'webchat'"
gcloud artifacts repositories describe webchat --project "$PROJECT" --location "$REGION" \
  >/dev/null 2>&1 || \
gcloud artifacts repositories create webchat --project "$PROJECT" \
  --repository-format=docker --location "$REGION"

echo "==> 3/4 Analytics pipeline (BigQuery datasets + Pub/Sub -> BQ + marts)"
if [ "${ENABLE_BQ_ANALYTICS:-0}" = "1" ]; then
  ./deploy/bigquery/setup.sh
else
  echo "    skipped (set ENABLE_BQ_ANALYTICS=1 to provision)"
fi

echo "==> 4/4 Build + deploy (Cloud Run)"
./deploy/deploy-cloudrun.sh

echo
echo "Bootstrap complete. Finish the IAM grants + Model Armor steps in"
echo "deploy/RUNBOOK.md (§4-5), then open the sample app printed above."
