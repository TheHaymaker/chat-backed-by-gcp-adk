"""PubSubSink — production event sink for the gateway.

Publishes each event as a JSON message to a Pub/Sub topic; a BigQuery
subscription (or Dataflow) lands them in `product_events` (DDL in
deploy/bigquery/schema.sql). Publish is fire-and-forget with local batching
via the client library; analytics must never block or break a chat turn.

Env: EVENTS_PUBSUB_TOPIC=projects/<p>/topics/<t>
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("gateway.events")


@dataclass
class PubSubSink:
    topic: str = field(default_factory=lambda: os.environ["EVENTS_PUBSUB_TOPIC"])
    publisher: Any = None            # injectable for tests

    def __post_init__(self) -> None:
        if self.publisher is None:
            from google.cloud import pubsub_v1
            self.publisher = pubsub_v1.PublisherClient(
                batch_settings=pubsub_v1.types.BatchSettings(
                    max_messages=100, max_latency=0.5))

    def emit(self, events: list[dict]) -> None:
        for e in events:
            try:
                data = json.dumps(e, separators=(",", ":"), default=str).encode()
                future = self.publisher.publish(
                    self.topic, data,
                    tenant_id=str(e.get("tenant_id", "")),
                    event_name=str(e.get("name", "")))
                future.add_done_callback(self._on_done)
            except Exception as err:            # never break the turn
                logger.error("pubsub emit failed: %s", err)

    @staticmethod
    def _on_done(future) -> None:
        try:
            future.result()
        except Exception as err:
            logger.error("pubsub publish failed: %s", err)


def default_sink():
    """EVENTS_PUBSUB_TOPIC set -> PubSubSink, else LogSink."""
    if os.environ.get("EVENTS_PUBSUB_TOPIC"):
        return PubSubSink()
    from gateway.runner import LogSink
    return LogSink()
