# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Router for the Tool Worker Pattern (ADR-036).
"""

from __future__ import annotations

import logging
from typing import Iterable

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.infra.redis import RedisLike

from kntgraph.core.event import Event

logger = logging.getLogger(__name__)


class ToolRouter:
    """
    Implements the Full Payload Fan-Out strategy.

    Observes outgoing events from systems and routes any 'tool.requested'
    events to the global tool queues (fmh:tools:<name>:queue), allowing
    the Tool Workers to execute without querying the agent's EventLog.
    """

    def __init__(self, redis: "RedisLike"):
        self._redis = redis

    async def route_batch(self, events: Iterable[Event]) -> None:
        """
        Inspects a batch of events and routes 'tool.requested' events.
        Errors during routing are logged but do not crash the caller,
        ensuring the main dispatcher loop continues.
        """
        for event in events:
            if event.event_type == "tool.requested":
                tool_name = event.data.get("tool")
                if not tool_name:
                    logger.warning(
                        f"tool.requested event {event.event_id} missing 'tool' in data"
                    )
                    continue

                stream_key = f"fmh:tools:{tool_name}:queue"
                try:
                    payload = event.to_json()
                    await self._redis.xadd(stream_key, {"payload": payload})
                    logger.debug(f"Routed tool.requested to {stream_key}")
                except Exception as e:
                    logger.error(
                        f"Failed to route tool.requested {event.event_id} to {stream_key}: {e}"
                    )
