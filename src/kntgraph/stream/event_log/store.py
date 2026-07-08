# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event_log.store -- The `EventLog` class.

Per-agent event log backed by Redis Streams. The
``EventLog`` is the ONLY writer of events. The Runner,
adapters, and external callers all go through
``append``. Read paths are ``read``, ``read_latest``, and
``iter_all`` (used by the World fold).

Composition
-----------

``EventLog`` is a thin orchestrator over an injected
``EventLogStorage``. The class no longer owns Redis
directly — all I/O is delegated to the storage adapter
(see ``kntgraph.infra.redis._event_log``).

The orchestrator handles:

  - validation (``validate_agent_id_for_redis``)
  - tenant ownership (``check_tenant_ownership``)
  - signature verification (``check_signature``)
  - resilience (``circuit_breaker``, ``append_backoff``)
  - structured logging and ``Result`` mapping

The storage handles:

  - wire format (codec, MAXLEN, idempotency keys)
  - Redis I/O

Why split
---------

Iteration 1 of the Redis adapter refactor (ADR-019).
Decomposes the original god class (4 stages + dispatch +
exception mapping + logging in a single 140-LOC method)
into:

  - preflight checks (Stages 1-3) — pure functions in
    ``validation.py``
  - dispatch (Stage 4) — delegated to storage
  - the ``EventLog`` itself becomes a 6-line orchestrator

The default ``require_signatures=False`` keeps existing
consumers working; production deployments targeting L1
MUST pass ``require_signatures=True``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Optional

import structlog

from ...core.event import Event
from ...core.result import Err, Ok, PersistenceError, Result
from ...resilience import CircuitBreaker
from ...resilience.timeout import BackoffPolicy
from .validation import (
    check_signature,
    check_tenant_ownership,
    validate_agent_id_for_redis,
)

if TYPE_CHECKING:
    from ...security import KeyRegistry


logger = structlog.get_logger()


def _build_default_backoff() -> BackoffPolicy:
    """
    Conservative default BackoffPolicy: 3 attempts
    (1 initial + 2 retries), 50ms base, 1s max,
    10s total budget. Mirrors the previous default
    kwargs (append_retry_attempts=2, ...).
    """
    return BackoffPolicy(
        max_attempts=3,
        base_delay=0.05,
        max_delay=1.0,
        max_total_seconds=10.0,
        retry_on=(
            asyncio.TimeoutError,
            ConnectionError,
            TimeoutError,
        ),
    )


class EventLog:
    """
    Per-agent event log — orchestrator over ``EventLogStorage``.

    This is the ONLY writer of events. The Runner, adapters,
    and external callers all go through ``append``. Read paths
    are ``read``, ``read_latest``, and ``iter_all`` (used by
    the World fold).

    The class no longer owns Redis directly; all I/O is
    delegated to the injected ``EventLogStorage``.

    ADR-016 L1 enforcement (PR 5):

      - ``key_registry`` — optional ``KeyRegistry`` for
        per-event signature verification at append time.
      - ``require_signatures`` — when True, an event with
        ``signature=None`` is rejected.
      - ``signature_warn_only`` — when True (and
        ``require_signatures=True``), unsigned / invalid
        events are logged at WARNING but accepted.
    """

    def __init__(
        self,
        storage,
        *,
        key_registry: Optional["KeyRegistry"] = None,
        require_signatures: bool = False,
        signature_warn_only: bool = False,
        circuit_breaker: Optional[CircuitBreaker] = None,
        append_timeout_seconds: float = 5.0,
        append_backoff: Optional[BackoffPolicy] = None,
    ) -> None:
        """
        Args:
            storage: the EventLog storage adapter (Redis impl
                or fake) — implements the ``EventLogStorage``
                Protocol (duck-typed).
            key_registry: optional ``KeyRegistry`` for
                signature verification.
            require_signatures: reject unsigned events
                (ADR-016 L1).
            signature_warn_only: log unsigned / invalid
                events at WARNING but accept them.
            circuit_breaker: optional
                ``CircuitBreaker`` applied to the append
                call. When the breaker is OPEN, ``append``
                returns ``Err`` immediately.
            append_timeout_seconds: per-attempt timeout for
                the storage call. Default 5s.
            append_backoff: the retry policy.
        """
        self._storage = storage
        # ADR-016 PR 5 enforcement hooks.
        self._key_registry = key_registry
        self._require_signatures = require_signatures
        self._signature_warn_only = signature_warn_only
        # Resilience wiring.
        self._circuit_breaker = circuit_breaker
        self._append_timeout_seconds = append_timeout_seconds
        self._append_backoff: Optional[BackoffPolicy] = append_backoff

    # ------------------------------------------------------------------ preflight

    def _preflight(self, event: Event) -> Optional[PersistenceError]:
        """
        Run the three preflight checks before delegating to
        storage. Returns ``None`` if all checks pass, or a
        ``PersistenceError`` ready to wrap in ``Err(...)``.
        """
        # Stage 1: agent_id validation
        agent_id_err = validate_agent_id_for_redis(event.agent_id)
        if agent_id_err is not None:
            logger.error(
                "event_log.append.invalid_agent_id",
                event_id=str(event.event_id),
                agent_id=event.agent_id,
                issue=agent_id_err,
            )
            return PersistenceError(agent_id_err)

        # Stage 2: tenant ownership
        tenant_err = check_tenant_ownership(event, _current_principal())
        if tenant_err is not None:
            logger.warning(
                "event_log.append.tenant_violation",
                event_id=str(event.event_id),
                agent_id=event.agent_id,
            )
            return tenant_err

        # Stage 3: signature
        sig_error = check_signature(
            event,
            key_registry=self._key_registry,
            require_signatures=self._require_signatures,
        )
        if sig_error is not None:
            if self._signature_warn_only:
                logger.warning(
                    "event_log.append.signature_issue",
                    event_id=str(event.event_id),
                    agent_id=event.agent_id,
                    issue=sig_error,
                )
            else:
                logger.error(
                    "event_log.append.signature_rejected",
                    event_id=str(event.event_id),
                    agent_id=event.agent_id,
                    issue=sig_error,
                )
                return PersistenceError(sig_error)

        return None

    # ------------------------------------------------------------------ write

    async def append(self, event: Event) -> Result[str, PersistenceError]:
        """
        Append an event to its agent's stream.

        Idempotent: if the same event_id is already in the
        log, the call is a no-op and the original stream entry
        id is returned.

        Returns the stream entry id of the (new or existing)
        event.
        """
        preflight_err = self._preflight(event)
        if preflight_err is not None:
            return Err(preflight_err)

        # Resilience wiring (circuit breaker / retry / direct).
        # The orchestrator owns the resilience policy; the
        # storage is a pure I/O boundary.
        async def _do_storage_call():
            return await self._storage.append(agent_id=event.agent_id, event=event)

        return await self._dispatch(_do_storage_call, event_id=str(event.event_id))

    async def _dispatch(
        self,
        do_call: Callable[[], Awaitable[Result[str, PersistenceError]]],
        *,
        event_id: str,
    ) -> Result[str, PersistenceError]:
        """Apply resilience (breaker / retry / direct) around the storage call.

        The storage returns a ``Result[str, PersistenceError]``.
        The dispatch layer wraps the call with timeout /
        retry / breaker policy. On timeout, retry exhaustion,
        or breaker rejection, we translate the raw exception
        into ``Err(PersistenceError(...))``.
        """
        from .dispatch import dispatch_redis_call

        # `dispatch_redis_call` works with byte-returning
        # callables (the legacy contract). For the refactored
        # EventLog (ADR-019), the storage returns a Result.
        # We adapt by wrapping the call: the inner function
        # returns the storage's Result; if it succeeds we
        # pass through; if it raises (timeout, breaker) we
        # surface as Err.
        async def _do_storage_call():
            r = await do_call()
            if r.is_err():
                raise _StorageError(r.err_value())
            return r.ok_value()

        try:
            result = await dispatch_redis_call(
                _do_storage_call,  # type: ignore[arg-type]
                circuit_breaker=self._circuit_breaker,
                append_backoff=self._append_backoff,
                append_timeout_seconds=self._append_timeout_seconds,
            )
        except _StorageError as e:
            return Err(e.persistence_error)
        if result.is_err():
            return Err(result.err_value())  # type: ignore[arg-type]
        logger.debug(
            "event_log.append.ok",
            event_id=event_id,
        )
        return Ok(str(result.ok_value()))

    async def append_batch(
        self,
        events: list[Event],
    ) -> Result[list[str], PersistenceError]:
        """
        Append multiple events. Each event is appended via
        ``append`` (idempotent). On error, returns the error
        without partial commit.
        """
        ids: list[str] = []
        for e in events:
            r = await self.append(e)
            if r.is_err():
                return Err(r.err_value())  # type: ignore[arg-type]
            ids.append(r.unwrap())
        return Ok(ids)

    # ------------------------------------------------------------------ read

    async def read(
        self,
        agent_id: str,
        start: str = "-",
        end: str = "+",
        count: Optional[int] = None,
    ) -> list[Event]:
        """Read events for one agent in [start, end] stream-id range."""
        return await self._storage.read(agent_id, start=start, end=end, count=count)

    async def read_latest(
        self,
        agent_id: str,
        n: int = 1,
    ) -> list[Event]:
        """Read the last N events for an agent (most recent first)."""
        return await self._storage.read_latest(agent_id, n)

    async def stream_len(self, agent_id: str) -> int:
        """Return the number of events in an agent's stream."""
        return await self._storage.stream_len(agent_id)

    async def iter_all(
        self,
        agent_ids: Optional[list[str]] = None,
        batch: int = 100,
    ) -> AsyncIterator[Event]:
        """
        Async iteration over events for the given agents (or
        all agents if None).
        """
        if agent_ids is None:
            agent_ids = await self._storage.list_agents()
        if agent_ids is None:
            return
        for aid in agent_ids:
            events = await self.read(aid, count=batch)
            for e in events:
                yield e

    async def list_agents(self) -> list[str]:
        """
        Return the list of agent_ids with at least one event.

        Iteration 5 (ADR-019): public delegation for the
        underlying storage. Replaces the legacy
        ``_list_agent_ids`` private method that callers
        could access only via the bound ``self._log._redis``
        (a privacy leak).
        """
        return await self._storage.list_agents()

    async def read_after_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        """
        Read events for one agent STRICTLY AFTER ``cursor``.

        Returns parsed ``Event`` objects in stream order. The
        cursor is interpreted as Redis stream id semantics:
        an empty string or ``"-"`` means "from the beginning".

        Iteration 5 (ADR-019): replaces the
        ``self._log._redis.xrange(...)`` access that the
        ``ReactiveDispatcher`` used to do. The dispatcher now
        talks to the EventLog via this public method instead
        of reaching through ``_redis``.

        Named ``read_after_cursor`` (not ``read_from``) to
        make the semantics explicit: the returned events are
        those whose stream id is strictly greater than
        ``cursor``. ``read_from`` could be ambiguous about
        whether ``cursor`` itself is included.
        """
        if not cursor:
            cursor = "-"

        if hasattr(self._storage, "read_with_cursor"):
            return await self._storage.read_with_cursor(agent_id, cursor)  # type: ignore

        # Fallback (never used in practice but good for type checkers)
        if cursor == "-" or cursor == "0-0":
            start = "-"
        else:
            start = f"({cursor}"
        events = await self.read(agent_id, start=start, end="+")
        if not events:
            return [], cursor
        return events, str(events[-1].event_id)

    # ------------------------------------------------------------------ delete

    async def delete_agent_stream(self, agent_id: str) -> None:
        """Removes the agent's stream. Used in tests / cleanup."""
        await self._storage.delete(agent_id)

    async def purge(self, agent_id: str) -> None:
        """Alias for ``delete_agent_stream``."""
        await self.delete_agent_stream(agent_id)


def _current_principal():
    """Read the bound principal from the ContextVar. Lazy import
    to avoid a cycle with the security module.
    """
    from ...security import principal_ctx

    return principal_ctx.get()


class _StorageError(Exception):
    """Internal: surfaces a storage-level PersistenceError through the dispatch layer."""

    def __init__(self, persistence_error) -> None:
        super().__init__(str(persistence_error))
        self.persistence_error = persistence_error


__all__ = [
    "EventLog",
    "_build_default_backoff",
]
