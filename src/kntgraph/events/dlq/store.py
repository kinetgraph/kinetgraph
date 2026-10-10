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

from typing import cast

import structlog

from ...core.result import Err, Ok, PersistenceError, Result
from ...infra.redis._dlq import (
    DLQ_AGENT_INDEX,
    PLACEHOLDER,
    DLQStorage,
    idem_key_for,
)
from ...infra.redis._errors import MemoryError
from ...infra.redis._translation import (
    translate_redis_call,
)
from .values import DeadLetterEvent, DLQReason

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

        The body is split into 3 per-step helpers
        (:meth:`_do_stream_append`, :meth:`_bump_reason_counter`,
        :meth:`_set_agent_head`) each routing through
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        Per DEBT §2.39: the catch list lives in the helper
        (single source of truth); the per-step split
        keeps the public ``append`` method free of
        ``except`` blocks.
        """
        event_id = str(dl_event.event.event_id)
        idem_key = idem_key_for(event_id, dl_event.reason.value)
        payload = dl_event.to_dict()

        # Step 1: write the stream entry + claim the
        # idempotency placeholder (single ``DLQStorage.append``
        # call). A transport failure here is fatal: the
        # caller MUST see ``Err``.
        stream_result = await self._do_stream_append(idem_key, payload)
        if stream_result.is_err():
            logger.error(
                "dlq.append.storage_error",
                event_id=event_id,
                reason=dl_event.reason.value,
                error=str(stream_result.err_value()),
            )
            return Err(
                PersistenceError(
                    f"Storage error in stream append: {stream_result.err_value()}"
                )
            )

        stream_id = stream_result.ok_value()
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

        # Step 2: bump the per-reason counter. The
        # counter is a hint (operator dashboard); a
        # transport failure here is logged and
        # swallowed at the helper boundary (the
        # operator's `dlq.get_stats` will see a stale
        # count, which is a self-correcting
        # inconsistency on the next ``purge``).
        await self._bump_reason_counter(dl_event.reason.value, event_id)

        # Step 3: set the per-agent head pointer
        # (HSETNX; first failure wins). Same hint
        # semantics as the counter — a transport
        # failure here is logged and swallowed.
        if stream_id is not None:
            await self._set_agent_head(dl_event.event.agent_id, stream_id, event_id)

        logger.warning(
            "dlq.append.ok",
            event_id=event_id,
            agent_id=dl_event.event.agent_id,
            reason=dl_event.reason.value,
            error=dl_event.error_message,
            retry_count=dl_event.retry_count,
            stream_id=stream_id,
        )
        return Ok(stream_id)  # type: ignore[arg-type]

    async def _do_stream_append(
        self, idem_key: str, payload: dict[str, str]
    ) -> Result[bytes, PersistenceError]:
        """Step 1 of ``append``: write the stream entry + claim
        the idempotency placeholder via ``DLQStorage.append``.

        The return type is ``bytes`` (``stream_id``) so the
        caller can short-circuit on ``PLACEHOLDER``. ``Err``
        surfaces a transport failure to the caller.
        """
        result = await self._storage.append(idem_key, payload)
        if result.is_err():
            return Err(
                PersistenceError(
                    f"Storage error in stream append: {result.err_value()}"
                )
            )
        return result  # type: ignore[return-value]

    async def _bump_reason_counter(self, reason: str, event_id: str) -> None:
        """Step 2 of ``append``: bump the per-reason counter.

        ``DLQStorage.bump_reason_counter`` returns
        ``Result[None, MemoryError]``; the typed ``Result``
        channel is consumed by :func:`translate_redis_call`
        which logs the ``Err`` and returns ``None`` (fail-soft
        — the counter is a hint, not a durability
        primitive).
        """
        result = await translate_redis_call(
            self._storage.bump_reason_counter(reason, 1),
            op_name="dlq_storage.bump_reason_counter.append",
            error_cls=MemoryError,
            key=DLQ_AGENT_INDEX,  # log context: namespace
            event_id=event_id,
            reason=reason,
        )
        if result.is_err():
            # The helper has already logged the
            # ``dlq_storage.bump_reason_counter.redis_error``
            # event. We do not propagate the Err to the
            # caller -- the counter is a hint (the
            # operator's ``dlq.get_stats`` will see a
            # stale count, which is self-correcting on
            # the next ``purge``).
            return
        return

    async def _set_agent_head(
        self, agent_id: str, stream_id: bytes, event_id: str
    ) -> None:
        """Step 3 of ``append``: set the per-agent head
        pointer via ``HSETNX`` (first failure wins).

        The ``DLQ_AGENT_INDEX`` is a hint, not a
        durability primitive. A transport failure
        here is logged and swallowed at the helper
        boundary (same ``fail_open`` semantics as
        :meth:`_bump_reason_counter`).
        """
        client = getattr(self._storage, "client", None)
        if client is None or not hasattr(client, "hsetnx"):
            # Some storage backends (in-memory
            # test doubles, future non-Redis adapters)
            # do not expose a raw ``client``. The
            # agent-index is a Redis-only optimisation;
            # the storage's ``append`` already wrote
            # the durable stream entry.
            return
        result = await translate_redis_call(
            client.hsetnx(DLQ_AGENT_INDEX, agent_id, stream_id),
            op_name="dlq_storage.set_agent_head.append",
            error_cls=MemoryError,
            key=DLQ_AGENT_INDEX,
            agent_id=agent_id,
            event_id=event_id,
        )
        if result.is_err():
            return
        return

    # ------------------------------------------------------------------ read

    async def get_event(
        self, event_id: str
    ) -> Result[DeadLetterEvent | None, PersistenceError]:
        """
        Read the first DLQ entry for a given event_id. The
        index keys are ``<event_id>:<reason>`` — we look up
        the first match via the storage.

        Returns ``Ok(None)`` when the index has no entry for
        the event_id (a clean miss, not a storage error).
        Returns ``Err(PersistenceError)`` when the storage
        layer reports a Redis-side failure (callers MUST
        inspect the error; AGENTS.md §6: "wrap-in-result,
        no fail-soft").
        """
        lookup = await self._storage.find_by_event_id(event_id)
        if lookup.is_err():
            logger.warning(
                "dlq.get_event.storage_error",
                event_id=event_id,
                error=str(lookup.err_value()),
            )
            return Err(
                PersistenceError(
                    f"Storage error in find_by_event_id: {lookup.err_value()}"
                )
            )
        stream_id = lookup.ok_value()
        if stream_id is None:
            return Ok(None)
        entry_result = await self._storage.read(stream_id)
        if entry_result.is_err():
            logger.warning(
                "dlq.get_event.read_failed",
                event_id=event_id,
                stream_id=stream_id,
                error=str(entry_result.err_value()),
            )
            return Err(
                PersistenceError(
                    f"Storage error in read({stream_id}): {entry_result.err_value()}"
                )
            )
        payload = entry_result.ok_value()
        if payload is None:
            return Ok(None)
        return Ok(self._build_event(cast("dict[str, str]", payload)))

    async def list_for_agent(
        self, agent_id: str, count: int = 100
    ) -> Result[list[DeadLetterEvent], PersistenceError]:
        """
        List DLQ entries for one agent. The agent index
        points to the FIRST failure; we forward-scan from
        there and filter by agent_id (the global DLQ stream
        may contain events for other agents in between).

        Returns ``Err(PersistenceError)`` on storage failure
        (callers MUST inspect the error; AGENTS.md §6).
        """
        result = await self._storage.list_for_agent(agent_id, count)
        if result.is_err():
            logger.warning(
                "dlq.list_for_agent.storage_error",
                agent_id=agent_id,
                error=str(result.err_value()),
            )
            return Err(
                PersistenceError(
                    f"Storage error in list_for_agent: {result.err_value()}"
                )
            )
        messages_payload = result.ok_value() or []
        return Ok(
            [
                self._build_event(cast("dict[str, str]", m))
                for m in messages_payload
                if m.get("agent_id") == agent_id
            ]
        )

    async def list_by_reason(
        self, reason: DLQReason, count: int = 100
    ) -> Result[list[DeadLetterEvent], PersistenceError]:
        """
        List DLQ entries with a given reason.

        Returns ``Err(PersistenceError)`` on storage failure
        (callers MUST inspect the error; AGENTS.md §6).
        """
        result = await self._storage.list_by_reason(reason.value, count)
        if result.is_err():
            logger.warning(
                "dlq.list_by_reason.storage_error",
                reason=reason.value,
                error=str(result.err_value()),
            )
            return Err(
                PersistenceError(
                    f"Storage error in list_by_reason: {result.err_value()}"
                )
            )
        messages_payload = result.ok_value() or []
        return Ok(
            [self._build_event(cast("dict[str, str]", m)) for m in messages_payload]
        )

    async def list_all(
        self, count: int = 100
    ) -> Result[list[DeadLetterEvent], PersistenceError]:
        """
        List the most recent DLQ entries regardless of
        agent or reason.

        Returns ``Err(PersistenceError)`` on storage failure
        (callers MUST inspect the error; AGENTS.md §6).
        """
        messages = await self._storage.list_all(count)
        if messages.is_err():
            logger.warning(
                "dlq.list_all.storage_error",
                error=str(messages.err_value()),
            )
            return Err(
                PersistenceError(f"Storage error in list_all: {messages.err_value()}")
            )
        messages_payload = messages.ok_value() or []
        return Ok(
            [self._build_event(cast("dict[str, str]", m)) for m in messages_payload]
        )

    # ------------------------------------------------------------------ stats

    async def get_stats(self) -> Result[dict, PersistenceError]:
        """
        Aggregate stats: total events, by_reason, by_agent.

        Returns ``Err(PersistenceError)`` on storage failure
        (callers MUST inspect the error; AGENTS.md §6). The
        ``Ok({...})`` channel always carries the shape
        ``{"total_events": int, "unique_agents": int,
        "by_reason": dict[str, int]}``.
        """
        result = await self._storage.get_stats()
        if result.is_err():
            logger.warning(
                "dlq.get_stats.storage_error",
                error=str(result.err_value()),
            )
            return Err(
                PersistenceError(f"Storage error in get_stats: {result.err_value()}")
            )
        return Ok(
            result.ok_value()  # type: ignore[arg-type]
            or {
                "total_events": 0,
                "unique_agents": 0,
                "by_reason": {},
            }
        )

    async def purge(self) -> Result[int, PersistenceError]:
        """
        Wipe the DLQ entirely. Used in tests and emergency
        recovery.
        """
        result = await self._storage.purge()
        if result.is_err():
            return Err(PersistenceError(f"Storage error: {result.err_value()}"))
        return Ok(result.ok_value() or 0)

    # ------------------------------------------------------------------ internal

    @staticmethod
    def _build_event(payload: dict) -> DeadLetterEvent:
        """Build a ``DeadLetterEvent`` from the parsed dict."""
        return DeadLetterEvent.from_dict(payload)
