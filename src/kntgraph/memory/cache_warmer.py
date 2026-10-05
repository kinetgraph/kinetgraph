# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Cache warmer — decouples the Consolidator (pure) from the
Redis cache (side-effecting).

The Consolidator emits `CacheRefreshRequest`s onto an in-memory
bus; the CacheWarmer subscribes and performs the actual
`refresh_cache` calls. The two never share a code path. This
keeps the cyclic system that runs every tick free of I/O and
makes the cache backend swappable without touching the
Consolidator.

The bus is intentionally **in-memory** (a `collections.deque`).
The EventLog is the only durable source of truth; cache
requests are housekeeping, not domain events, so they do not
need to survive a process restart. If the bus is dropped on
restart, the EventLog + read-through pattern in the managers
guarantees correctness on the next miss.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import structlog

from ..core.result import Ok, Result
from ..infra.redis._errors import MemoryError
from .profile import ProfileManager
from .session import SessionManager

if TYPE_CHECKING:
    # Import apenas para type-check; evita ciclo em runtime
    # entre continuity.py e este módulo.
    from .continuity import ContinuityManager

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class PumpOutcome:
    """
    Per-batch outcome of :meth:`CacheWarmer.pump_once`.

    ``ok`` is the number of refreshes that succeeded;
    ``failed`` is the number that reported an error.
    ``errors`` carries the actual error objects so the
    operator-facing dashboard can surface them (no
    fail-soft logging without a structured payload).

    ``PumpOutcome.failed == 0`` means the whole batch
    succeeded; callers can short-circuit on that.
    """

    ok: int = 0
    failed: int = 0
    errors: tuple[MemoryError, ...] = field(default_factory=tuple)


CacheRefreshKind = Literal["session", "profile", "continuity"]


@dataclass(frozen=True, slots=True)
class CacheRefreshRequest:
    """
    Request to refresh the cache for a single memory agent.

    Carries the **kind** (so the warmer dispatches to the
    right manager) and the **id** (session_id or
    (tenant_id, user_id) tuple — encoded here as two
    positional strings, since the field is fixed-width).

    `kind == "session"`    →  `id1 = session_id`, `id2 = ""`
    `kind == "profile"`    →  `id1 = tenant_id`, `id2 = user_id`
    `kind == "continuity"` →  `id1 = tenant_id`, `id2 = user_id`

    Flattening to two strings keeps the dataclass
    `frozen=True, slots=True` and avoids a union field that
    would force `cast` at every call site.
    """

    kind: CacheRefreshKind
    id1: str
    id2: str = ""


class CacheRefreshBus:
    """
    In-memory FIFO queue of `CacheRefreshRequest`s.

    Thread/async-safe under cooperative multitasking: a single
    producer (the Consolidator on the Runner's tick) and a
    single consumer (the CacheWarmer in its own task) is the
    expected setup. The bus does not lock; the async
    scheduler guarantees that `publish` and `drain` are not
    interleaved in the typical case.

    For multi-consumer / multi-producer setups, swap the
    deque for `asyncio.Queue` (interface-compatible: both
    expose `append` / `popleft` semantically, but the
    Queue has its own locking).
    """

    __slots__ = ("_queue",)

    def __init__(self) -> None:
        self._queue: deque[CacheRefreshRequest] = deque()

    def publish(self, request: CacheRefreshRequest) -> None:
        """Enqueue a refresh request."""
        self._queue.append(request)

    def drain(self) -> list[CacheRefreshRequest]:
        """
        Atomically remove and return all queued requests.

        Returns a list (snapshot), not the deque — the caller
        should not retain a reference that outlives the
        function call. New requests published after `drain`
        stays in the queue.
        """
        items = list(self._queue)
        self._queue.clear()
        return items

    def __len__(self) -> int:
        return len(self._queue)

    def __repr__(self) -> str:
        return f"CacheRefreshBus(pending={len(self._queue)})"


class CacheWarmer:
    """
    Subscribes to a `CacheRefreshBus` and applies each
    request to the appropriate cache (session JSON,
    profile Hash or continuity Hash — ADR-014).

    `pump_once()` is the single sink for the cache-write
    I/O. It is idempotent: re-running it on the same bus
    with no new requests is a no-op; re-running it on
    the same requests is also a no-op because the
    underlying `refresh_cache` is itself idempotent
    (rebuilds from the EventLog).
    """

    def __init__(
        self,
        bus: CacheRefreshBus,
        session_manager: SessionManager,
        profile_manager: ProfileManager,
        continuity_manager: ContinuityManager | None = None,
    ) -> None:
        self._bus = bus
        self._sessions = session_manager
        self._profiles = profile_manager
        # Continuity manager é opcional por compatibilidade
        # com callers existentes; quando presente, dispatch
        # adicional em `pump_once`. Veja ADR-014.
        self._continuity = continuity_manager

    async def pump_once(self) -> Result[PumpOutcome, MemoryError]:
        """
        Drain the bus and apply all pending requests.

        ADR-068 §3.4 P4: every request flows through
        :meth:`BaseShortTermMemory.refresh_cache_incremental`.
        That method reads the fold cursor from the
        parallel Redis key ``<cache_key>:fold_cursor``;
        if the cursor is missing it falls back to the
        full :meth:`BaseShortTermMemory.refresh_cache`
        which seeds the cursor for the next call. The
        dispatcher does not need to carry a cursor on
        the request — the cache owns that state.

        Returns a ``Result[PumpOutcome, MemoryError]``
        wrapping the per-batch outcome. ``Ok(PumpOutcome)``
        always reports ``ok + failed == len(requests)``
        (or both zero when the bus was empty).
        ``Err(MemoryError)`` surfaces when the bus
        itself reports a failure — at the moment the
        bus is an in-memory ``deque`` that cannot
        fail, so the ``Err`` branch is reserved for
        future storage-backed buses (ADR-068 §3.4
        stretch goal). Per-request refresh errors
        surface inside ``PumpOutcome.errors`` so a
        single bad request never aborts the batch
        (the batch is best-effort by design).

        The per-request resilience is the same one the
        legacy ``except Exception`` provided, but the
        failure payload is now typed (``MemoryError``)
        and propagated via the ``Result`` channel
        instead of lost in a logger warning.
        """
        requests = self._bus.drain()
        if not requests:
            return Ok(PumpOutcome())

        successes = 0
        failures: list[MemoryError] = []
        for req in requests:
            try:
                if req.kind == "session":
                    result = await self._sessions.refresh_cache_incremental(req.id1)
                elif req.kind == "profile":
                    result = await self._profiles.refresh_cache_incremental(
                        req.id1, req.id2
                    )
                elif req.kind == "continuity":
                    if self._continuity is None:
                        logger.warning(
                            "cache_warmer.continuity_unconfigured",
                            id1=req.id1,
                            id2=req.id2,
                        )
                        # Continuity not configured ⇒ the request is
                        # acknowledged but skipped, NOT counted as a
                        # failure (matches the legacy contract: the
                        # operator opted out of the tier).
                        continue
                    result = await self._continuity.refresh_cache_incremental(
                        req.id1, req.id2
                    )
                else:  # pragma: no cover - guarded by Literal
                    logger.warning(
                        "cache_warmer.unknown_kind",
                        kind=str(req.kind),
                        id1=req.id1,
                        id2=req.id2,
                    )
                    failures.append(
                        MemoryError(f"unknown CacheRefreshKind {req.kind!r}")
                    )
                    continue
            except Exception as exc:  # noqa: BLE001
                # Defensive: a buggy implementation might raise
                # instead of returning Err. Convert to a typed
                # ``MemoryError`` so the per-batch outcome stays
                # consistent and the dispatch loop never aborts.
                logger.warning(
                    "cache_warmer.refresh_raised",
                    kind=req.kind,
                    id1=req.id1,
                    id2=req.id2,
                    error=str(exc),
                )
                failures.append(
                    MemoryError(
                        f"refresh_cache_incremental for "
                        f"({req.kind}, {req.id1!r}, {req.id2!r}) "
                        f"raised: {exc}"
                    )
                )
                continue

            if result.is_err():
                err = result.err_value()
                # The base layer wraps storage errors as
                # ``PersistenceError``; the warmer's public
                # contract says ``MemoryError``. The two are
                # distinguishable but both belong in the
                # warm-side error channel -- use the bare
                # ``MemoryError`` text for the metrics sink
                # and let the dashboard dedupe.
                logger.warning(
                    "cache_warmer.refresh_failed",
                    kind=req.kind,
                    id1=req.id1,
                    id2=req.id2,
                    error=str(err),
                )
                failures.append(
                    MemoryError(
                        f"refresh_cache_incremental for "
                        f"({req.kind}, {req.id1!r}, {req.id2!r}) "
                        f"failed: {err}"
                    )
                )
                continue
            successes += 1

        return Ok(
            PumpOutcome(
                ok=successes,
                failed=len(failures),
                errors=tuple(failures),
            )
        )

    async def run_forever(self, interval: float | None = None) -> None:
        """
        Cooperative loop: pump the bus every `interval`
        seconds. Cancelled cleanly on `asyncio.CancelledError`.

        `interval=None` reads the ``KNT_WARMER_PUMP_INTERVAL``
        knob (ADR-068 §3.8; default 0.25). Explicit values keep
        the legacy behaviour.

        Intended for production deployments where the
        warmer runs as a long-lived background task.
        """
        if interval is None:
            from kntgraph.infra.config import fresh_settings

            interval = fresh_settings().warmer_pump_interval
        try:
            while True:
                await self.pump_once()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            # Drain once more on shutdown so the last batch
            # of requests is not lost.
            await self.pump_once()
            raise
