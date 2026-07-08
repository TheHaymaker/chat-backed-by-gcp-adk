#!/usr/bin/env bash
# One-time BigQuery + Pub/Sub setup for the analytics pipeline.
set -euo pipefail
PROJECT="${PROJECT:?}"; REGION="${REGION:-us-central1}"

bq mk --project_id "$PROJECT" -d --location "$REGION" webchat_analytics || true
bq mk --project_id "$PROJECT" -d --location "$REGION" agent_telemetry || true
sed "s/\${PROJECT}/$PROJECT/g" deploy/bigquery/schema.sql | bq query --project_id "$PROJECT" --use_legacy_sql=false

gcloud pubsub topics create webchat-events --project "$PROJECT" || true
gcloud pubsub subscriptions create webchat-events-to-bq --project "$PROJECT" \
  --topic webchat-events \
  --bigquery-table="$PROJECT.webchat_analytics.product_events" \
  --use-table-schema || true

sed "s/\${PROJECT}/$PROJECT/g" deploy/bigquery/marts.sql | bq query --project_id "$PROJECT" --use_legacy_sql=false
echo "Gateway env: EVENTS_PUBSUB_TOPIC=projects/$PROJECT/topics/webchat-events"
