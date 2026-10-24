# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Router for the Tool Worker Pattern (ADR-036).

``ToolRouter`` forwards each outgoing ``tool.<name>.requested``
event to the per-tool queue stream ``knt:tools:<name>:queue``,
so the workers run the tool without querying the agent's
EventLog.

``dedup=True`` opts in to a per-``(tool_name, event_id)``
idempotency index: the router claims a slot before the
``XADD`` and skips the write when the index already holds a
final stream_id. The default ``dedup=False`` keeps the wire
format of a deployment that never opted in.

The dedup index lives in its own Redis namespace
(``knt:tools:<name>:idem:<id>``), separate from the EventLog
dedup (``knt:eventids:<id>``), so a shared Redis does not let
a tool-queue entry collide with an EventLog entry.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.infra.redis import RedisLike

from redis import exceptions as redis_exceptions

from kntgraph.core.event import Event
from kntgraph.core.tool_event import ToolEventKind, parse_tool_event
from kntgraph.core.world.components import ToolCallTTL
from kntgraph.infra.redis._prefix import validate_prefix
from kntgraph.infra.redis._tools import (
    DEDUP_TTL_SECONDS_DEFAULT,
    tool_event_id_key,
    tool_queue_key,
)
from kntgraph.tools._router_dedup import route_with_dedup
from kntgraph.tools._ttl import payload_with_ttl

logger = logging.getLogger(__name__)


class ToolRouter:
    """Forwards outgoing tool requests to the tool queues.

    Each ``tool.<name>.requested`` event lands on the stream
    ``knt:tools:<name>:queue``.

    ``key_prefix`` namespaces the queue key, so two services
    sharing one Redis do not cross-talk. The default ``""``
    keeps the unprefixed key.

    ``dedup`` turns on the per-``event_id`` idempotency index
    at the ``XADD`` boundary: a request whose ``event_id`` was
    already routed is skipped. ``dedup_ttl_seconds`` bounds the
    index size (default 1 h).

    ``tool_ttls`` is the dispatcher's per-tool TTL, the
    fallback for a tool that declares no timeout of its own.
    ``publish_timeouts()`` adopts the timeouts the registered
    tools declared, and those win.
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        key_prefix: str = "",
        dedup: bool = False,
        dedup_ttl_seconds: int = DEDUP_TTL_SECONDS_DEFAULT,
        tool_ttls: ToolCallTTL | None = None,
    ):
        # Validated once here; ``_stream_key`` does not re-check.
        validate_prefix(key_prefix)
        self._redis = redis
        self._key_prefix = key_prefix
        # Dedup is opt-in; ``False`` keeps the wire format of a
        # deployment that never opted in.
        self._dedup = dedup
        self._dedup_ttl_seconds = dedup_ttl_seconds
        # The dispatcher's per-tool TTL, the fallback for a tool
        # that declares none. ``None`` leaves the TTL unset.
        self._tool_ttls = tool_ttls
        # The timeouts the registered tools declared. They win
        # over ``_tool_ttls``.
        self._published_timeouts: dict[str, float] = {}

    def _stream_key(self, tool_name: str) -> str:
        """The namespaced queue key for ``tool_name``."""
        return tool_queue_key(self._key_prefix, tool_name)

    def _idem_key(self, tool_name: str, event_id: str) -> str:
        """The namespaced dedup-index key for
        ``(tool_name, event_id)``. Used only when
        ``dedup=True``.
        """
        return tool_event_id_key(self._key_prefix, tool_name, event_id)

    async def _route_one_with_dedup(
        self,
        *,
        tool_name: str,
        event_id: str,
        stream_key: str,
        payload_dict: dict,
    ) -> bool:
        """Claim the idempotency slot for one event and
        append it.

        The hit / transport-failure / success decision lives in
        ``tools._router_dedup``; this supplies the router's
        state.
        """
        return await route_with_dedup(
            redis=self._redis,
            idem_key=self._idem_key(tool_name, event_id),
            stream_key=stream_key,
            payload=payload_dict,
            dedup_ttl_seconds=self._dedup_ttl_seconds,
            tool_name=tool_name,
            event_id=event_id,
        )

    def _extract_tool_name(self, event: Event) -> str | None:
        """The tool name a request event targets, or ``None``.

        Accepts the canonical ``tool.<name>.requested`` and the
        legacy ``tool.requested`` with ``data["tool"]``. Every
        other event yields ``None``.
        """
        if (
            event.event_type == "tool.requested"
            and isinstance(event.data, dict)
            and "tool" in event.data
        ):
            return str(event.data["tool"])
        parsed = parse_tool_event(event.event_type)
        if parsed is not None and parsed.kind == ToolEventKind.REQUESTED:
            return parsed.tool_name
        return None

    def publish_timeouts(self, timeouts: Mapping[str, float]) -> None:
        """Adopt the timeouts the registered tools declared.

        The operator calls this with
        ``worker_manager.published_timeouts()`` after registering
        the tools. Each request then carries its own TTL, so the
        reaper reclaims it on that threshold instead of the
        global ``reaper_idle_time``.

        A declared timeout wins over ``tool_ttls``: the tool
        knows its own latency budget, and one declaration is
        the point.
        """
        self._published_timeouts.update(timeouts)

    async def route_batch(self, events: Iterable[Event]) -> None:
        """Routes the batch's ``tool.*.requested`` events to
        the tool queues.

        Routing errors are logged, not raised: the dispatcher's
        loop must survive one bad event.

        With ``dedup=True`` each request claims an idempotency
        slot before the ``XADD``. A dedup hit is skipped. A
        transport error falls through to the bare ``XADD``, so a
        transient Redis hiccup never costs a request.
        """
        for event in events:
            tool_name = self._extract_tool_name(event)

            if tool_name:
                stream_key = self._stream_key(tool_name)
                payload_dict = {
                    "payload": payload_with_ttl(
                        event=event,
                        tool_name=tool_name,
                        tool_ttls=self._tool_ttls,
                        published=self._published_timeouts,
                    )
                }
                try:
                    if self._dedup:
                        handled = await self._route_one_with_dedup(
                            tool_name=tool_name,
                            event_id=str(event.event_id),
                            stream_key=stream_key,
                            payload_dict=payload_dict,
                        )
                        # ``handled`` means the dedup helper either
                        # appended the event or skipped a duplicate.
                        # Only a transport-class error returns False,
                        # and only then does the bare ``xadd`` run, so
                        # the dispatcher never drops a request.
                        if handled:
                            continue
                    await self._redis.xadd(stream_key, payload_dict)
                    logger.debug(
                        f"worker.router.appended tool={tool_name} "
                        f"event_id={event.event_id}"
                    )
                except (
                    redis_exceptions.RedisError,
                    ConnectionError,
                    OSError,
                    TimeoutError,
                    TypeError,
                    ValueError,
                ) as e:
                    logger.error(
                        f"Failed to route tool.{tool_name}.requested "
                        f"{event.event_id} to {stream_key}: {e}"
                    )
