# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Router for the Tool Worker Pattern (ADR-036).

Dedup-by-``event_id`` (ADR-083)
-------------------------------

``ToolRouter(redis, *, dedup=True)`` consults a per-``(tool_name,
event_id)`` SET-based idempotency index BEFORE every ``XADD`` and
SKIPS the write when the index already holds a final stream_id.
The feature is opt-in (default ``dedup=False``) so the
pre-ADR-083 wire format stays byte-for-byte identical when the
operator has not opted in (backoffice ADR-013 §1.1: 67 entries
with the same ``event_id`` accumulated in 11 minutes on the
2026-10-14 incident, blocking the real user request behind 66
replays for 5.5 hours).

The dedup index lives in a separate Redis namespace
(``knt:tools:<name>:idem:<id>``) from the EventLog dedup
(``knt:eventids:<id>``) so a shared Redis does not let a
tool-queue entry collide with an EventLog entry. ADR-076
namespace prefix threads through the index key the same
way it threads through the queue key.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.infra.redis import RedisLike

from redis import exceptions as redis_exceptions

from kntgraph.core.event import Event
from kntgraph.core.result import PersistenceError
from kntgraph.core.tool_event import ToolEventKind, parse_tool_event
from kntgraph.infra.redis._prefix import validate_prefix
from kntgraph.infra.redis._tools import (
    DEDUP_TTL_SECONDS_DEFAULT,
    claim_tool_queue_slot,
    tool_event_id_key,
    tool_queue_key,
)

logger = logging.getLogger(__name__)


class ToolRouter:
    """
    Implements the Full Payload Fan-Out strategy.

    Observes outgoing events from systems and routes any 'tool.*.requested'
    events to the global tool queues (knt:tools:<name>:queue), allowing
    the Tool Workers to execute without querying the agent's EventLog.

    ``key_prefix`` (ADR-076, DEBT §2.35) namespaces the
    queue key so two services sharing one Redis do not
    cross-talk at the tool-dispatcher boundary. The
    default ``""`` preserves the pre-ADR-076 wire format
    byte-for-byte.

    ``dedup`` (ADR-083) opts in to per-``event_id`` dedup at
    the ``XADD`` boundary. The default ``False`` keeps the
    pre-ADR-083 wire format; ``True`` adds a
    ``SET key NX EX`` round-trip per ``XADD`` and SKIPS the
    ``XADD`` when the index already holds a final
    stream_id. ``dedup_ttl_seconds`` bounds the index
    size; the default (``DEDUP_TTL_SECONDS_DEFAULT`` = 1h)
    covers the backoffice 2026-10-14 re-emit window with
    margin to spare.
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        key_prefix: str = "",
        dedup: bool = False,
        dedup_ttl_seconds: int = DEDUP_TTL_SECONDS_DEFAULT,
    ):
        # Validated once at construction (same pattern as
        # ``RedisPool.__init__`` in
        # ``infra/redis/_pool.py``); the per-call path
        # (``_stream_key`` below) does not re-check.
        validate_prefix(key_prefix)
        self._redis = redis
        self._key_prefix = key_prefix
        # ADR-083: dedup is opt-in. ``False`` keeps the
        # pre-083 wire format byte-for-byte; ``True``
        # consults the SET index before every ``XADD``.
        self._dedup = dedup
        self._dedup_ttl_seconds = dedup_ttl_seconds

    def _stream_key(self, tool_name: str) -> str:
        """Compose the namespaced Stream key for ``tool_name``."""
        return tool_queue_key(self._key_prefix, tool_name)

    def _idem_key(self, tool_name: str, event_id: str) -> str:
        """Compose the namespaced dedup-index key for
        ``(tool_name, event_id)`` (ADR-083 §3.1). Only used
        when ``dedup=True``.
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
        """Run the dedup path for one event (ADR-083 §3.2).

        Returns ``True`` when the event was handled
        (appended or skipped by dedup); the caller does not
        need to call the bare ``xadd``. Returns ``False``
        when the dedup helper returned a transport-class
        error and the caller must fall through to the bare
        ``xadd`` so a transient Redis hiccup never costs
        the dispatcher a real request.

        Extracted from ``route_batch`` to keep the parent
        method at CC <= 10 (ratchet rule).
        """
        result = await claim_tool_queue_slot(
            self._redis,
            idem_key=self._idem_key(tool_name, event_id),
            stream_key=stream_key,
            payload=payload_dict,
            maxlen=None,
            ttl_seconds=self._dedup_ttl_seconds,
        )
        if result.is_ok():
            logger.debug(
                f"worker.router.appended tool={tool_name} "
                f"event_id={event_id} stream_id={result.ok_value()}"
            )
            return True
        err = result.err_value()
        if isinstance(err, PersistenceError) and ("tool_queue_dedup_hit" in str(err)):
            logger.debug(
                f"worker.router.skipped_dedup tool={tool_name} event_id={event_id}"
            )
            return True
        # Transport-class error -- caller falls through to bare xadd.
        logger.error(
            f"worker.router.dedup_error.falling_through_to_xadd "
            f"tool={tool_name} event_id={event_id} error={err}"
        )
        return False

    def _extract_tool_name(self, event: Event) -> str | None:
        """Extract the tool name from a tool-request event
        (canonical ``tool.<name>.requested`` or legacy
        ``tool.requested`` + ``data["tool"]``). Returns
        ``None`` for non-tool events.

        Extracted from ``route_batch`` to keep the parent
        method at CC <= 10 (ratchet rule).
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

    async def route_batch(self, events: Iterable[Event]) -> None:
        """
        Inspects a batch of events and routes 'tool.*.requested' events.
        Errors during routing are logged but do not crash the caller,
        ensuring the main dispatcher loop continues.

        ADR-083: when ``dedup=True``, every
        ``tool.*.requested`` event flows through
        :func:`claim_tool_queue_slot` (the three-phase
        ``SET NX`` dedup pattern) before the ``XADD``. A
        dedup hit (``Err(PersistenceError("tool_queue_dedup_hit"))``)
        is logged as ``worker.router.skipped_dedup`` and the
        loop continues -- the dedup is the desired outcome
        of a re-emit, not an error. A transport error
        (``Err(PersistenceError("redis: ..."))``) falls
        through to the bare ``XADD`` so a transient Redis
        hiccup never costs the dispatcher a real request.
        """
        for event in events:
            tool_name = self._extract_tool_name(event)

            if tool_name:
                stream_key = self._stream_key(tool_name)
                payload_dict = {"payload": event.to_json()}
                try:
                    if self._dedup:
                        handled = await self._route_one_with_dedup(
                            tool_name=tool_name,
                            event_id=str(event.event_id),
                            stream_key=stream_key,
                            payload_dict=payload_dict,
                        )
                        # ``handled`` means the dedup helper either
                        # appended the event (Ok) or skipped a duplicate
                        # (Err dedup_hit). Only when ``handled`` is
                        # False (transport-class error) do we fall
                        # through to the bare ``xadd`` so the
                        # dispatcher never drops a request.
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
