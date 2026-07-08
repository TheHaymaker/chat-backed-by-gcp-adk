-- Session mart: joins agent traces to product events on session_id.
CREATE OR REPLACE VIEW `${PROJECT}.webchat_analytics.v_sessions` AS
WITH product AS (
  SELECT tenant_id, session_id,
         MIN(ts) AS started_at,
         COUNTIF(name = 'message_sent')        AS user_messages,
         COUNTIF(name = 'scheduler_offered')   AS schedulers_shown,
         COUNTIF(name = 'booking_completed')   AS bookings,
         COUNTIF(name = 'booking_cancelled')   AS cancellations,
         COUNTIF(name = 'lead_captured')       AS leads,
         COUNTIF(name = 'kb_no_answer')        AS kb_misses,
         COUNTIF(name = 'envelope_sanitized')  AS sanitized_envelopes
  FROM `${PROJECT}.webchat_analytics.product_events`
  GROUP BY tenant_id, session_id
),
agent AS (
  SELECT JSON_VALUE(attributes, '$.session_id') AS session_id,
         SUM(CAST(JSON_VALUE(payload, '$.usage.total_tokens') AS INT64)) AS total_tokens,
         AVG(CAST(JSON_VALUE(payload, '$.latency_ms') AS FLOAT64))       AS avg_llm_latency_ms,
         COUNT(*)                                                        AS llm_calls
  FROM `${PROJECT}.agent_telemetry.agent_events`
  WHERE event_type = 'LLM_RESPONSE'
  GROUP BY session_id
)
SELECT p.*, a.total_tokens, a.avg_llm_latency_ms, a.llm_calls
FROM product p LEFT JOIN agent a USING (session_id);

-- Booking funnel per tenant per day.
CREATE OR REPLACE VIEW `${PROJECT}.webchat_analytics.v_booking_funnel` AS
SELECT tenant_id, DATE(started_at) AS day,
       COUNT(*)                          AS sessions,
       COUNTIF(schedulers_shown > 0)     AS reached_scheduler,
       COUNTIF(bookings > 0)             AS converted,
       SAFE_DIVIDE(COUNTIF(bookings > 0), COUNT(*)) AS conversion_rate,
       SAFE_DIVIDE(SUM(total_tokens), NULLIF(COUNTIF(bookings > 0), 0))
                                         AS tokens_per_booking
FROM `${PROJECT}.webchat_analytics.v_sessions`
GROUP BY tenant_id, day;
