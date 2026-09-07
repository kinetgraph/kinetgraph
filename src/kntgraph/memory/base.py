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
  - Incremental refresh (ADR-068 §3.4 P4):
    ``refresh_cache_incremental(...)`` folds only the
    EventLog delta since the cache's last published
    fold cursor, instead of the full stream prefix.

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

P4 design — fold cursor lives on a parallel Redis key
-----------------------------------------------------

The fold cursor (the Redis Stream id of the last event
consumed by the fold that wrote the cache) is stored on
a **parallel Redis key**:
``<cache_key>:fold_cursor``. It is intentionally NOT a
field inside the cache payload:

  - the payload stays bit-identical to the legacy wire
    format (``HSET`` / JSON ``SET``), so legacy caches
    written by the cold ``refresh_cache`` path (and by
    the ``Projector.write_cache`` helper) remain valid
    input for the incremental path — no migration is
    required;
  - the cursor is observable via
    ``KEYS knt:*:fold_cursor`` / a single ``SCAN MATCH``
    for operational debugging;
  - the payload and the cursor can have independent
    TTLs (Continuity's sliding TTL renews the cache on
    every write; the cursor can either share that TTL
    or stay longer, independently).

The base owns three small helpers for the parallel key
(``_fold_cursor_key``, ``_read_fold_cursor``,
``_write_fold_cursor``); subclasses inherit them as-is.
Only the **incremental path** is P4 — the cold
``refresh_cache`` keeps the legacy shape and only writes
the cursor key as an optimisation when the fold
produced a result.

**Domain / infra separation**: the helpers delegate to the
storage Protocol (``ShortMemoryStorage.read_fold_cursor``
/ ``write_fold_cursor``). The base does NOT touch the
raw Redis client — that would re-couple the domain
layer to the wire format, undoing the ADR-019 split.
Each concrete adapter (Session / Profile / Continuity)
owns the right Redis primitive + TTL policy for the
parallel cursor.

What lives in the base
----------------------
Everything that is identical across the three managers:

  - Constructor wiring (EventLog + ShortMemoryStorage + TTL).
  - ``read(key_parts)`` (the public read-through).
  - ``refresh_cache(key_parts)`` (the public full rebuild).
  - ``refresh_cache_incremental(key_parts)`` (P4 hot path).
  - The orchestration of cache → fold → cache.

What lives in the subclass
--------------------------
The shape of the state and the format of the cache:

  - ``cache_key(*parts)``     — Redis key for the cache entry.
  - ``_read_cache(key)``      — decode the cache → StateT.
  - ``_write_cache(key, state)`` — encode + write StateT → cache.
  - ``_fold_from_log(*parts)`` — pure fold over EventLog events.
  - (optional) ``_fold_incremental(parts, existing, delta_events)``
    — merge a delta onto an existing state (default
    implementation in the base falls back to a full
    refold over the existing state + delta, which is
    correct but more expensive than the per-event
    merge).

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

from ..core.event import Event
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

# Suffix that distinguishes the fold-cursor key from
# the cache key. The base uses it to derive
# ``<cache_key>:fold_cursor``; subclasses do not touch it.
# Public so tests / ops tools can ``SCAN MATCH`` for it.
FOLD_CURSOR_SUFFIX = ":fold_cursor"


class BaseShortTermMemory(ABC, Generic[StateT]):
    """
    Abstract base for the RAB "short-memory" shape, FMH-flavoured.

    A subclass MUST implement four methods:

      1. ``cache_key(*parts)``        — Redis key for the cache entry.
      2. ``_read_cache(key)``         — decode the cache → StateT.
      3. ``_write_cache(key, state)`` — encode + write StateT → cache.
      4. ``_fold_from_log(*parts)``   — pure fold over EventLog events.

    The base class provides the orchestration (read-through,
    write-through, refresh, incremental refresh). The
    subclass owns the shape.

    Iteration 2 (ADR-019): the Redis impls live in
    ``kntgraph.infra.redis._memory``. The base class
    consumes the ``ShortMemoryStorage`` Protocol — never
    ``redis.asyncio`` directly.

    Iteration 3 (ADR-068 §3.4 P4): the base owns the
    fold-cursor helpers (parallel Redis key,
    :data:`FOLD_CURSOR_SUFFIX`); subclasses inherit them
    as-is and only override the optional
    :meth:`_fold_incremental` hook when they want true
    delta-onto-state merge (default = full refold over the
    existing state + delta, which is correct but more
    expensive than the per-event merge).
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

        Side effect (P4 optimisation): after writing the
        cache payload, also stamps the fold cursor onto
        the parallel ``<key>:fold_cursor`` key so the next
        incremental call can take the warm path. The
        cursor write is best-effort (a failure is logged
        and ignored — the cache itself is still
        consistent).
        """
        folded = await self._fold_from_log(*key_parts)
        if folded is not None:
            key = self.cache_key(*key_parts)
            await self._write_cache_for_key(key, folded)
            cursor = await self._log.latest_stream_id(self.agent_id_for(*key_parts))
            if cursor is not None:
                await self._write_fold_cursor(key, cursor)

    async def refresh_cache_incremental(self, *key_parts: str) -> None:
        """
        Rebuild the cache by folding ONLY the EventLog
        delta since the last published fold cursor
        (ADR-068 §3.4 P4). Falls back to
        :meth:`refresh_cache` when the cursor is absent
        (cold cache or legacy cache written without one).

        Behaviour matrix:

        - no fold cursor at the parallel key → fall
          back to the full rebuild so the cursor is
          seeded;
        - cursor present, delta empty → no-op (no
          Redis write);
        - cursor present, delta non-empty → read the
          current state, merge the delta (via
          :meth:`_fold_incremental` when overridden,
          otherwise via the default full refold over
          the cached state + delta), persist the new
          state AND the new cursor.

        The hot path runs from the ``CacheWarmer`` pump
        loop. The cold path (``refresh_cache``) seeds
        the cursor on the first call.
        """
        key = self.cache_key(*key_parts)
        cursor = await self._read_fold_cursor(key)
        if cursor is None:
            await self.refresh_cache(*key_parts)
            return

        agent_id = self.agent_id_for(*key_parts)
        delta, new_cursor = await self._log.read_after_cursor(agent_id, cursor)
        if not delta:
            return

        existing = await self._read_state_for_incremental(key, *key_parts)
        if existing is None:
            # Cache disappeared between cursor read and
            # here — treat as cold and rebuild from
            # scratch (the cursor will be re-seeded).
            await self.refresh_cache(*key_parts)
            return

        merged = await self._fold_incremental(key_parts, existing, delta)
        if merged is None:
            await self.refresh_cache(*key_parts)
            return

        await self._write_cache_for_key(key, merged)
        await self._write_fold_cursor(key, new_cursor)

    # ------------------------------------------------------------------ protected

    # ------------------------------------------------------------------ P4 helpers

    def _fold_cursor_key(self, key: str) -> str:
        """
        Derive the parallel fold-cursor key from the cache
        key. Public so tests can assert and ops tools can
        SCAN for it.
        """
        return key + FOLD_CURSOR_SUFFIX

    async def _read_fold_cursor(self, key: str) -> str | None:
        """
        Read the fold cursor stored at the parallel key.
        Returns ``None`` on cache miss or transport
        failure (a missing cursor means "next call must
        use the cold path" — there is no ambiguity).

        The cursor is a plain string value (NOT a Hash
        field and NOT a JSON entry); the parallel-key
        convention keeps it independent of the cache
        payload shape (Hash vs JSON). The base delegates
        to the storage; it does not touch the raw
        Redis client (domain/infra separation, ADR-019).
        """
        try:
            return await self._storage.read_fold_cursor(self._fold_cursor_key(key))
        except Exception as e:
            logger.warning(
                "short_term.fold_cursor.read_failed",
                key=key,
                error=str(e),
            )
            return None

    async def _write_fold_cursor(self, key: str, cursor: str) -> None:
        """
        Persist the fold cursor on the parallel key.
        Best effort: a transport failure is logged at
        WARNING and swallowed. The cache payload itself
        is still consistent (the cursor is metadata,
        not the memory state).

        TTL matches the cache payload: the two keys
        MUST expire together, else the cursor could
        outlive the cache and the warm path would
        anchor on a missing cache. The hot path falls
        back to cold on cache miss, so the
        inconsistency self-corrects, but matching TTLs
        is the honest contract.
        """
        cursor_key = self._fold_cursor_key(key)
        ttl = self._ttl if self._ttl and self._ttl > 0 else None
        try:
            await self._storage.write_fold_cursor(cursor_key, cursor, ttl_seconds=ttl)
        except Exception as e:
            logger.warning(
                "short_term.fold_cursor.write_failed",
                key=key,
                error=str(e),
            )

    async def _read_state_for_incremental(
        self, key: str, *key_parts: str
    ) -> StateT | None:
        """
        Decode the current cache payload into a StateT
        for the incremental path. Returns ``None`` on
        miss or decode failure — the caller treats that
        as "cold cache, rebuild from scratch".

        Distinct from :meth:`_read_cache` only in that
        the Result is unwrapped here for the caller —
        errors are logged at the base, not surfaced.
        """
        result = await self._read_cache(key, *key_parts)
        if result.is_err():
            logger.warning(
                "short_term.cache.read_failed",
                key=key,
                error=str(result.err_value()),
            )
            return None
        return result.ok_value()

    async def _fold_incremental(
        self,
        key_parts: tuple[str, ...],
        existing: StateT,
        delta: list[Event],
    ) -> StateT | None:
        """
        Merge a non-empty list of new events onto an
        existing cached state and return the merged
        state (ADR-068 §3.4 P4).

        **Base default** returns ``None`` and signals
        the caller to fall back to the full
        :meth:`refresh_cache`. Subclasses with a
        structured fold handler table (Session's
        append-only MESSAGE / ENDED, Profile's
        preference set/unset, Continuity's handler
        table) MAY override this method to apply the
        delta directly onto the state — that's the
        path that unlocks the per-event merge
        optimisation. Without the override, the
        incremental path collapses to a cold refresh
        (still correct, just no cheaper than the
        legacy code path).

        Returning a state means "this is the merged
        state; persist it and stamp the new cursor".
        Returning ``None`` triggers the caller's
        fallback to :meth:`refresh_cache`.
        """
        del key_parts, existing, delta  # base default: no merge
        return None

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
    "CachePayload",
    "FOLD_CURSOR_SUFFIX",
    "StateT",
]
