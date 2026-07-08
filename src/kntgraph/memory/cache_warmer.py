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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Deque, Literal, Optional

import structlog

from .profile import ProfileManager
from .session import SessionManager

if TYPE_CHECKING:
    # Import apenas para type-check; evita ciclo em runtime
    # entre continuity.py e este módulo.
    from .continuity import ContinuityManager

logger = structlog.get_logger()


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
        self._queue: Deque[CacheRefreshRequest] = deque()

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
        continuity_manager: Optional["ContinuityManager"] = None,
    ) -> None:
        self._bus = bus
        self._sessions = session_manager
        self._profiles = profile_manager
        # Continuity manager é opcional por compatibilidade
        # com callers existentes; quando presente, dispatch
        # adicional em `pump_once`. Veja ADR-014.
        self._continuity = continuity_manager

    async def pump_once(self) -> int:
        """
        Drain the bus and apply all pending requests.
        Returns the number of refreshes applied.
        """
        requests = self._bus.drain()
        if not requests:
            return 0

        for req in requests:
            try:
                if req.kind == "session":
                    await self._sessions.refresh_cache(req.id1)
                elif req.kind == "profile":
                    await self._profiles.refresh_cache(req.id1, req.id2)
                elif req.kind == "continuity":
                    if self._continuity is None:
                        logger.warning(
                            "cache_warmer.continuity_unconfigured",
                            id1=req.id1,
                            id2=req.id2,
                        )
                        continue
                    await self._continuity.refresh_cache(req.id1, req.id2)
            except Exception as e:  # noqa: BLE001
                # I/O failure on one request must not abort
                # the rest of the batch. Log and continue.
                logger.warning(
                    "cache_warmer.refresh_failed",
                    kind=req.kind,
                    id1=req.id1,
                    id2=req.id2,
                    error=str(e),
                )
        return len(requests)

    async def run_forever(self, interval: float = 0.25) -> None:
        """
        Cooperative loop: pump the bus every `interval`
        seconds. Cancelled cleanly on `asyncio.CancelledError`.

        Intended for production deployments where the
        warmer runs as a long-lived background task.
        """
        try:
            while True:
                await self.pump_once()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            # Drain once more on shutdown so the last batch
            # of requests is not lost.
            await self.pump_once()
            raise
