# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
BaseShortTermMemory — shared contract for SessionManager,
ProfileManager and ContinuityManager.

This is the FMH-flavoured implementation of the Redis Agent
Builder (RAB) "short-memory" shape. The RAB cookbook defines
short-term memory as a per-conversation/per-user store with
read, write, and a clear. FMH adapts the pattern to the
event-sourced model:

  - The EventLog (Redis Streams) is the source of truth.
  - The memory cache (Redis Hash or JSON) is a TTL cache
    that the manager maintains. The cache is ALWAYS
    rebuildable from the EventLog; a cold or missing cache
    is not a failure.
  - Read-through: ``read`` tries the cache first, falls
    back to a fold over the EventLog, and refreshes the
    cache on miss.
  - Write-through: ``write_cache(...)`` writes the given
    state to the cache directly. Used by the Projector
    (see ``kntgraph.memory.consolidation.Projector``).
  - Refresh: ``refresh_cache(...)`` rebuilds the cache by
    folding the EventLog. Used by the CacheWarmer
    adapter (see ``kntgraph.memory.cache_warmer``).

The three concrete tiers (ADR-014 §2.1):

  - ``SessionManager``    — JSON cache, TTL ≤ 24h, single-part
                            identity ``(session_id,)``.
  - ``ProfileManager``    — Hash cache, sem TTL, two-part
                            identity ``(tenant_id, user_id)``.
                            Config estável da PME.
  - ``ContinuityManager`` — Hash cache, TTL sliding
                            (renovado a cada write), two-part
                            identity ``(tenant_id, user_id)``.
                            Estado-de-uso recente. PII
                            hash-only, LGPD ``cleared``.

Iteration 2 (ADR-019): the three Redis impls are wired
via the ``ShortMemoryStorage`` Protocol (see
``kntgraph.infra.redis._memory``). The base class no
longer talks to ``redis.asyncio`` directly; the storage
adapters own the wire format.

What lives in the base
----------------------
Everything that is identical across the three managers:

  - Constructor wiring (EventLog + ShortMemoryStorage + TTL).
  - ``read(key_parts)`` (the public read-through).
  - ``refresh_cache(key_parts)`` (the public rebuild).
  - The orchestration of cache → fold → cache.

What lives in the subclass
--------------------------
The shape of the state and the format of the cache:

  - ``cache_key(*parts)``     — Redis key for the cache entry.
  - ``_read_cache(key)``      — decode the cache → StateT.
  - ``_write_cache(key, state)`` — encode + write StateT → cache.
  - ``_fold_from_log(*parts)`` — pure fold over EventLog events.

Why a base class, not a Protocol
--------------------------------
A Protocol would document the contract but not remove the
duplication. The whole point of this refactor is to share
the cache orchestration code; only an abstract base class
delivers that.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Generic, Optional, TypeVar, Union

import structlog

from ..core.result import Result
from ..stream.event_log import EventLog

if TYPE_CHECKING:
    from ..infra.redis._errors import MemoryDecodeError
    from ..infra.redis._memory import ShortMemoryStorage

logger = structlog.get_logger()

# Generic state type. Subclasses parameterise with their own
# state class (SessionState, ProfileState, ...).
StateT = TypeVar("StateT")

# Cache payload: either a JSON-encoded string (for
# ``SET key value``) or a Hash mapping (for ``HSET``).
# The concrete choice is per-tier (SessionManager uses
# JSON; ProfileManager and ContinuityManager use Hash).
CachePayload = Union[str, dict[str, str]]


class BaseShortTermMemory(ABC, Generic[StateT]):
    """
    Abstract base for the RAB "short-memory" shape, FMH-flavoured.

    A subclass MUST implement four methods:

      1. ``cache_key(*parts)``        — Redis key for the cache entry.
      2. ``_read_cache(key)``         — decode the cache → StateT | None.
      3. ``_write_cache(key, state)`` — encode + write StateT → cache.
      4. ``_fold_from_log(*parts)``   — pure fold over EventLog events.

    The base class provides the orchestration (read-through,
    write-through, refresh). The subclass owns the shape.

    Iteration 2 (ADR-019): the Redis impls live in
    ``kntgraph.infra.redis._memory``. The base class
    consumes the ``ShortMemoryStorage`` Protocol — never
    ``redis.asyncio`` directly.
    """

    # The Redis key prefix for this kind of memory. Subclasses
    # may set this in the constructor; it is exposed here as
    # a class attribute for tests and introspection.
    key_prefix: str = ""

    # The EventLog agent_id prefix. The Consolidator's parser
    # consults this attribute to classify an EventLog
    # agent_id, so renaming the value here is enough to change
    # the wire format.
    agent_id_prefix: str = ""

    def __init__(
        self,
        event_log: EventLog,
        storage: "ShortMemoryStorage",
        *,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        self._log = event_log
        self._storage = storage
        self._ttl = ttl_seconds

    # ------------------------------------------------------------------ id

    @classmethod
    def agent_id_for(cls, *parts: str) -> str:
        """
        Build the EventLog agent_id for this memory.

        Default implementation joins the parts with ``:``
        (single source of truth — Profile and Continuity
        both use ``"profile:tenant-A:user-1"`` shape).
        Subclasses may override for non-``:`` separators,
        but in practice the default is enough.
        """
        return f"{cls.agent_id_prefix}{':'.join(parts)}"

    @classmethod
    @abstractmethod
    def cache_key(cls, *parts: str) -> str:
        """
        Build the Redis cache key for a logical id.

        The arguments are the parts of the identity (e.g. a
        single session_id, or a (tenant_id, user_id) pair).
        The implementation is responsible for joining them
        with the right separator and prefix.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ public

    async def read(self, *key_parts: str) -> Optional[StateT]:
        """
        Read the current state for the given identity.
        Tries the cache first; on miss, folds the EventLog
        and refreshes the cache.

        This is the standard read-through pattern. It is
        idempotent and safe to call from any caller.

        `key_parts` are the components of the identity. For
        a session, it is ``(session_id,)``. For a profile,
        it is ``(tenant_id, user_id)``. The base resolves
        the Redis key via ``cache_key(*key_parts)``.

        Cache errors (decoded via the ``ShortMemoryStorage``
        Protocol's ``Result`` contract) are logged and
        treated as a miss: a transient Redis blip MUST NOT
        fail the read-through. The fold-fallback still
        succeeds against the EventLog.
        """
        key = self.cache_key(*key_parts)
        cache_result = await self._read_cache(key, *key_parts)
        if cache_result.is_err():
            logger.warning(
                "short_term.cache.read_failed",
                key=key,
                error=str(cache_result.err_value()),
            )
        else:
            cached = cache_result.ok_value()
            if cached is not None:
                return cached
        folded = await self._fold_from_log(*key_parts)
        if folded is not None:
            await self._write_cache_for_key(key, folded)
        return folded

    async def refresh_cache(self, *key_parts: str) -> None:
        """
        Rebuild the cache for one identity by folding the
        EventLog. Idempotent: if no events exist, this is a
        no-op.

        Public API: the ``CacheWarmer`` adapter calls this in
        response to a ``CacheRefreshRequest``. The method is
        named without the leading underscore precisely
        because it is part of the cross-module contract.
        """
        folded = await self._fold_from_log(*key_parts)
        if folded is not None:
            key = self.cache_key(*key_parts)
            await self._write_cache_for_key(key, folded)

    # ------------------------------------------------------------------ protected

    @abstractmethod
    async def _read_cache(
        self, key: str, *key_parts: str
    ) -> "Result[Optional[StateT], MemoryDecodeError]":
        """
        Decode the cache entry at ``key`` into a StateT.
        Return ``Ok(None)`` if the entry is missing or
        ``Err(MemoryDecodeError)`` on a malformed payload or
        Redis-side failure.

        Subclasses implement this with the right storage
        primitive (GET for JSON, HGETALL for Hash, etc.).
        Errors are surfaced (not swallowed) so the base
        class can log and fall through to the EventLog fold.

        ``key_parts`` is the same identity the base used to
        compute ``key``; subclasses that encode the identity
        in the Redis key (e.g. Profile's
        ``knt:profile:{tenant_id}:{user_id}``) but NOT in
        the Hash payload can use ``key_parts`` to reconstruct
        the identity in the decoded state.
        """
        raise NotImplementedError

    @abstractmethod
    async def _fold_from_log(self, *key_parts: str) -> Optional[StateT]:
        """
        Pure fold: events → StateT.

        Reads the relevant events from the EventLog and
        reduces them to a StateT. Returns None if no events
        exist for the identity (i.e. the memory has not been
        initialised yet).

        Subclasses implement this with their own event
        vocabulary. The implementation is expected to be
        pure: the only state it reads is the events it is
        handed.
        """
        raise NotImplementedError

    @abstractmethod
    def _serialize_for_cache(self, state: StateT) -> CachePayload:
        """
        Encode a StateT into the cache payload.

        For JSON-based caches, return a dict (the caller
        will json.dumps it). For Hash-based caches, return
        a dict[str, str] (the caller will HSET each pair).

        The base class centralises the actual storage call
        (and the TTL handling) so subclasses do not repeat
        the boilerplate.
        """
        raise NotImplementedError

    async def _write_cache_for_key(self, key: str, state: StateT) -> None:
        """
        Internal write-through helper. The base class calls
        this whenever it needs to push a StateT to the cache
        (read-through refresh, fold-then-refresh, etc.).

        Public callers should use the subclass-specific
        ``write_cache(state)`` or ``write_cache(..., state)``
        that resolves the identity components into a key
        first. This helper takes the already-resolved key.

        Errors from the storage (``MemoryError``) are
        swallowed with a WARNING log — the cache is a hint,
        not the source of truth. The EventLog is the
        authoritative state.
        """
        payload = self._serialize_for_cache(state)
        ttl = self._ttl if self._ttl and self._ttl > 0 else None
        result = await self._storage.put_record(key, payload, ttl_seconds=ttl)
        if result.is_err():
            logger.warning(
                "short_term.cache.write_failed",
                key=key,
                error=str(result.err_value()),
            )


__all__ = [
    "BaseShortTermMemory",
    "StateT",
]
