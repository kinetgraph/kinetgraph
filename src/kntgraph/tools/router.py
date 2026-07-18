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
from kntgraph.core.tool_event import ToolEventKind, parse_tool_event

logger = logging.getLogger(__name__)


class ToolRouter:
    """
    Implements the Full Payload Fan-Out strategy.

    Observes outgoing events from systems and routes any 'tool.*.requested'
    events to the global tool queues (knt:tools:<name>:queue), allowing
    the Tool Workers to execute without querying the agent's EventLog.
    """

    def __init__(self, redis: "RedisLike"):
        self._redis = redis

    async def route_batch(self, events: Iterable[Event]) -> None:
        """
        Inspects a batch of events and routes 'tool.*.requested' events.
        Errors during routing are logged but do not crash the caller,
        ensuring the main dispatcher loop continues.
        """
        for event in events:
            tool_name = None
            if (
                event.event_type == "tool.requested"
                and isinstance(event.data, dict)
                and "tool" in event.data
            ):
                tool_name = str(event.data["tool"])
            else:
                parsed = parse_tool_event(event.event_type)
                if parsed is not None and parsed.kind == ToolEventKind.REQUESTED:
                    tool_name = parsed.tool_name

            if tool_name:
                stream_key = f"knt:tools:{tool_name}:queue"
                try:
                    payload = event.to_json()
                    await self._redis.xadd(stream_key, {"payload": payload})
                    logger.debug(f"Routed tool.{tool_name}.requested to {stream_key}")
                except Exception as e:
                    logger.error(
                        f"Failed to route tool.{tool_name}.requested {event.event_id} to {stream_key}: {e}"
                    )
