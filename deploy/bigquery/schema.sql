-- product_events: widget + gateway business events (Pub/Sub -> BQ subscription
-- with "write metadata" off, message data as JSON payload).
CREATE TABLE IF NOT EXISTS `${PROJECT}.webchat_analytics.product_events` (
  name        STRING NOT NULL,
  props       JSON,
  tenant_id   STRING,
  session_id  STRING,
  turn_id     STRING,
  source      STRING,            -- widget | agent | gateway
  ts          TIMESTAMP
)
PARTITION BY DATE(ts)
CLUSTER BY tenant_id, name
OPTIONS (partition_expiration_days = 400);

-- agent_events is created by the ADK BigQuery Agent Analytics plugin in the
-- agent_telemetry dataset; enable its per-event-type views per plugin docs.
