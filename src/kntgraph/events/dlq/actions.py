# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
dlq.actions -- High-level DLQ operations (`reprocess`, `discard`).

``DeadLetterActions`` is composed with ``DeadLetterQueue``
(storage + facade) so the caller can use a single handle
for storage + actions. Iteration 5 (ADR-019): the actions
delegate Redis I/O to the ``DLQStorage`` Protocol — no
direct ``redis.asyncio`` access.

The split mirrors the boundary the original
``DeadLetterQueue`` already had:

  - ``reprocess(event_id)``: returns the original ``Event``
    so the caller can re-append it to the EventLog.
    The DLQ entry is removed (XDEL) only on success of
    the surrounding reprocess workflow.
  - ``discard(event_id)``: removes the DLQ entry without
    reprocessing (e.g. poison pill).

Both actions share ``_drop_entry(event_id, reason)``,
the private helper that removes a DLQ entry and
decrements the per-reason counter.
"""

from __future__ import annotations

import structlog

from ...core.event import Event
from ...core.result import Err, Ok, PersistenceError, Result
from ...infra.redis._dlq import (
    DLQStorage,
)
from .store import DeadLetterQueue
from .values import DLQReason


logger = structlog.get_logger()


class DeadLetterActions:
    """
    High-level DLQ operations. Composed with a
    ``DeadLetterQueue`` (storage + facade) so the
    caller can use a single DLQ handle.

    Iteration 5: ``actions`` no longer subclasses the
    queue — composition is cleaner and avoids the
    god-method trap of inheriting all 9 queue methods
    just to call ``self.get_event``.
    """

    def __init__(
        self,
        queue: DeadLetterQueue | None = None,
        *,
        storage: DLQStorage | None = None,
    ) -> None:
        """
        Construct the actions handle.

        Two entry points:

          - ``DeadLetterActions(queue=...)`` — preferred. Pass
            the existing queue (caller already has one).
          - ``DeadLetterActions(storage=...)`` — convenience
            for callers that don't need the queue methods.
            A queue is constructed internally.
        """
        if queue is not None:
            self._queue = queue
            self._storage = queue._storage
        elif storage is not None:
            self._storage = storage
            self._queue = None
        else:
            raise TypeError("DeadLetterActions requires `queue=` or `storage=`.")

    async def reprocess(self, event_id: str) -> Result[Event, PersistenceError]:
        """
        Return the original Event so the caller can re-append
        it to the EventLog. Removes the entry from the DLQ
        stream and the per-event_id index. The agent index
        is left alone (it points to the agent's first DLQ
        entry; other entries may still exist).

        Note: the entry is removed (XDEL) only on success of
        the surrounding reprocess workflow. If the caller
        fails to re-append, the entry is still in the DLQ.
        """
        # Look up the entry across all reasons (the
        # ``get_event`` helper searches by event_id).
        entry = await self._find_entry(event_id)
        if entry is None:
            return Err(PersistenceError(f"No DLQ entry for event_id={event_id}"))
        await self._drop_entry(event_id, entry.reason)
        logger.info(
            "dlq.reprocess.ok",
            event_id=event_id,
            reason=entry.reason.value,
        )
        return Ok(entry.event)

    async def discard(self, event_id: str) -> Result[bool, PersistenceError]:
        """
        Remove the DLQ entry without reprocessing (e.g.
        poison pill). Same handling of indexes as
        ``reprocess``.
        """
        entry = await self._find_entry(event_id)
        if entry is None:
            return Err(PersistenceError(f"No DLQ entry for event_id={event_id}"))
        await self._drop_entry(event_id, entry.reason)
        logger.info(
            "dlq.discard.ok",
            event_id=event_id,
            reason=entry.reason.value,
        )
        return Ok(True)

    async def _find_entry(self, event_id: str):
        """Look up a DLQ entry by event_id (across all reasons)."""
        if self._queue is not None:
            return await self._queue.get_event(event_id)
        # No queue — fall back to direct storage scan.
        lookup = await self._storage.find_by_event_id(event_id)
        if lookup.is_err() or lookup.ok_value() is None:
            return None
        stream_id = lookup.ok_value()
        assert stream_id is not None  # narrowed above
        entry_result = await self._storage.read(stream_id)
        if entry_result.is_err() or entry_result.ok_value() is None:
            return None
        return self._build_event(entry_result.ok_value())

    async def _drop_entry(self, event_id: str, reason: DLQReason) -> None:
        """
        Remove the DLQ entry for ``(event_id, reason)`` and
        decrement the per-reason counter.

        Steps:

          1. Read the stream id from ``DLQ_EVENT_INDEX``
             (``<event_id>:<reason>`` → stream_id).
          2. Storage XDELs the stream entry (only when the
             stream id is a real entry, not the
             placeholder written during a concurrent
             insert).
          3. Storage HDELs the per-event_id index entry.
          4. Decrement the per-reason counter (best-effort;
             a counter failure does not roll back the XDEL
             because the inconsistency is recoverable: a
             future ``purge`` rebuilds the counters).

        The agent index is left alone — it points to the
        agent's FIRST failure, which may be a different
        entry. Removing the wrong pointer would hide the
        original failure from ``list_for_agent``.
        """
        lookup = await self._storage.read_index(event_id, reason.value)
        if lookup.is_err():
            logger.warning(
                "dlq.drop_entry.read_index_failed",
                event_id=event_id,
                reason=reason.value,
                error=str(lookup.err_value()),
            )
            return
        stream_id = lookup.ok_value() or ""

        drop_result = await self._storage.drop_entry(event_id, reason.value, stream_id)
        if drop_result.is_err():
            logger.warning(
                "dlq.drop_entry.drop_failed",
                event_id=event_id,
                reason=reason.value,
                error=str(drop_result.err_value()),
            )
            return

        # Best-effort counter decrement.
        bump = await self._storage.bump_reason_counter(reason.value, -1)
        if bump.is_err():
            logger.warning(
                "dlq.drop_entry.counter_failed",
                event_id=event_id,
                reason=reason.value,
                error=str(bump.err_value()),
            )

    @staticmethod
    def _build_event(payload):
        from .values import DeadLetterEvent

        return DeadLetterEvent.from_dict(payload)


__all__ = ["DeadLetterActions"]
