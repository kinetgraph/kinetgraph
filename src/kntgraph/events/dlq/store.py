# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
dlq.store -- `DeadLetterQueue` class (domain facade).

The store is a thin composition over the ``DLQStorage``
Protocol. It owns the wire-format decode (dict →
``DeadLetterEvent``) and the high-level idempotency
semantics (``<event_id>:<reason>`` dedup boundary).
All Redis I/O is delegated to the storage.

Iteration 5 (ADR-019): the store no longer talks to
``redis.asyncio`` directly. The I/O lives in
``kntgraph.infra.redis._dlq.RedisDLQStorage``.

Idempotency protocol
--------------------

  1. Caller computes ``idem_key = <event_id>:<reason>``.
  2. Caller calls ``await queue.append(dl_event)``.
  3. Storage writes the stream entry (XADD) + claims the
     per-event_id slot (HSETNX with PLACEHOLDER + HSET
     final).
  4. Storage bumps the per-reason counter (best-effort).
  5. Storage sets the per-agent head pointer (HSETNX).
  6. Storage returns the stream id.

A concurrent insert is signalled by ``PLACEHOLDER``
appearing in the index; the storage returns
``Ok("PLACEHOLDER")`` so the caller can decide whether
to surface this as an error or treat it as a normal
replay.

The high-level operations (``reprocess``, ``discard``) live
in ``dlq.actions`` so the store stays focused on storage +
read, and actions can compose with the EventLog and
external callers.
"""

from __future__ import annotations

from typing import Optional

import structlog

from ...core.result import Err, Ok, PersistenceError, Result
from ...infra.redis._dlq import (
    DLQ_AGENT_INDEX,
    DLQStorage,
    PLACEHOLDER,
    idem_key_for,
)
from .values import DLQReason, DeadLetterEvent


logger = structlog.get_logger()


# Re-export the legacy constants for back-compat. The
# single source of truth is now ``infra.redis._dlq``.
__all__ = (
    ["DeadLetterQueue"]
    + [
        # Sub-package re-exports — see ``infra.redis._dlq``.
    ]
)


class DeadLetterQueue:
    """
    Append-only DLQ. Idempotent on (event_id, reason).

    Iteration 5: thin facade over ``DLQStorage``. The
    queue builds ``DeadLetterEvent`` from the parsed dicts
    returned by the storage.
    """

    def __init__(
        self,
        storage: DLQStorage,
        *,
        maxlen: int = 1_000_000,
    ) -> None:
        self._storage = storage
        self._maxlen = maxlen

    # ------------------------------------------------------------------ write

    async def append(self, dl_event: DeadLetterEvent) -> Result[str, PersistenceError]:
        """
        Append a DLQ entry. Idempotent on (event_id, reason):
        a second call with the same event and reason returns
        the original stream id without creating a duplicate.

        The dedup boundary is ``<event_id>:<reason>``. The
        storage bumps the per-reason counter and sets the
        per-agent head pointer (HSETNX) automatically.
        """
        event_id = str(dl_event.event.event_id)
        idem_key = idem_key_for(event_id, dl_event.reason.value)
        payload = dl_event.to_dict()

        result = await self._storage.append(idem_key, payload)
        if result.is_err():
            logger.error(
                "dlq.append.storage_error",
                event_id=event_id,
                reason=dl_event.reason.value,
                error=str(result.err_value()),
            )
            return Err(PersistenceError(f"Storage error: {result.err_value()}"))

        stream_id = result.ok_value()
        # ``PLACEHOLDER`` indicates a concurrent insert
        # in flight; surface as a recoverable result so
        # the caller can decide.
        if stream_id == PLACEHOLDER:
            logger.debug(
                "dlq.append.idempotent_skip",
                event_id=event_id,
                reason=dl_event.reason.value,
            )
            return Ok(PLACEHOLDER)

        # Bump the per-reason counter (best-effort; storage
        # already handles its own errors).
        counter = await self._storage.bump_reason_counter(dl_event.reason.value, 1)
        if counter.is_err():
            logger.warning(
                "dlq.append.counter_failed",
                event_id=event_id,
                reason=dl_event.reason.value,
                error=str(counter.err_value()),
            )

        # The agent index points to the FIRST failure of
        # the agent, so subsequent failures don't overwrite
        # the head pointer.
        try:
            await self._storage.client.hsetnx(
                DLQ_AGENT_INDEX,
                dl_event.event.agent_id,
                stream_id,
            )
        except Exception as e:  # pragma: no cover
            logger.warning(
                "dlq.append.agent_index_failed",
                event_id=event_id,
                error=str(e),
            )

        logger.warning(
            "dlq.append.ok",
            event_id=event_id,
            agent_id=dl_event.event.agent_id,
            reason=dl_event.reason.value,
            error=dl_event.error_message,
            retry_count=dl_event.retry_count,
            stream_id=stream_id,
        )
        return Ok(stream_id)

    # ------------------------------------------------------------------ read

    async def get_event(self, event_id: str) -> Optional[DeadLetterEvent]:
        """
        Read the first DLQ entry for a given event_id. The
        index keys are ``<event_id>:<reason>`` — we look up
        the first match via the storage.
        """
        lookup = await self._storage.find_by_event_id(event_id)
        if lookup.is_err():
            logger.warning(
                "dlq.get_event.storage_error",
                event_id=event_id,
                error=str(lookup.err_value()),
            )
            return None
        stream_id = lookup.ok_value()
        if stream_id is None:
            return None
        entry_result = await self._storage.read(stream_id)
        if entry_result.is_err() or entry_result.ok_value() is None:
            return None
        return self._build_event(entry_result.ok_value())

    async def list_for_agent(
        self, agent_id: str, count: int = 100
    ) -> list[DeadLetterEvent]:
        """
        List DLQ entries for one agent. The agent index
        points to the FIRST failure; we forward-scan from
        there and filter by agent_id (the global DLQ stream
        may contain events for other agents in between).
        """
        result = await self._storage.list_for_agent(agent_id, count)
        if result.is_err():
            logger.warning(
                "dlq.list_for_agent.storage_error",
                agent_id=agent_id,
                error=str(result.err_value()),
            )
            return []
        return [
            self._build_event(m)
            for m in result.ok_value()
            if m.get("agent_id") == agent_id
        ]

    async def list_by_reason(
        self, reason: DLQReason, count: int = 100
    ) -> list[DeadLetterEvent]:
        """
        List DLQ entries with a given reason.
        """
        result = await self._storage.list_by_reason(reason.value, count)
        if result.is_err():
            logger.warning(
                "dlq.list_by_reason.storage_error",
                reason=reason.value,
                error=str(result.err_value()),
            )
            return []
        return [self._build_event(m) for m in result.ok_value()]

    async def list_all(self, count: int = 100) -> list[DeadLetterEvent]:
        messages = await self._storage.list_all(count)
        if messages.is_err():
            logger.warning(
                "dlq.list_all.storage_error",
                error=str(messages.err_value()),
            )
            return []
        return [self._build_event(m) for m in messages.ok_value()]

    # ------------------------------------------------------------------ stats

    async def get_stats(self) -> dict:
        """
        Returns aggregate stats: total events, by_reason,
        by_agent. On storage error returns an empty dict
        (caller can detect via the ``"error"`` key set by
        the storage layer).
        """
        result = await self._storage.get_stats()
        if result.is_err():
            logger.warning(
                "dlq.get_stats.storage_error",
                error=str(result.err_value()),
            )
            return {
                "total_events": 0,
                "unique_agents": 0,
                "by_reason": {},
            }
        return result.ok_value()

    async def purge(self) -> Result[int, PersistenceError]:
        """
        Wipe the DLQ entirely. Used in tests and emergency
        recovery.
        """
        result = await self._storage.purge()
        if result.is_err():
            return Err(PersistenceError(f"Storage error: {result.err_value()}"))
        return Ok(result.ok_value())

    # ------------------------------------------------------------------ internal

    @staticmethod
    def _build_event(payload: dict) -> DeadLetterEvent:
        """Build a ``DeadLetterEvent`` from the parsed dict."""
        return DeadLetterEvent.from_dict(payload)
