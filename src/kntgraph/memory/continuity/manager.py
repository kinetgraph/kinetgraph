# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.manager -- The `ContinuityManager` class.

Thin glue between:

  - the persistence layer (`BaseShortTermMemory`,
    `EventLog`),
  - the cache codec (`cache_codec.read_cache`,
    `cache_codec.serialize_for_cache`),
  - the pure event builders (`recorders.build_*_event`),
  - the PII gate (`pii.check_pii_hash` — used by
    `recorders.entity`, returns ``Result[None, ...]``
    so the manager composes via ``.bind``).

The 5 `record_*` public methods (`create`,
`record_tool_used`, `record_entity_seen`,
`record_category_chosen`, `clear`) are short: they
validate the *manager-level* invariants (non-empty
slot, non-empty kind, etc.), delegate event
construction to the recorder, and run the
`_emit_and_refresh` flow (append to EventLog + rebuild
cache). The PII gate is inside the entity recorder, so
it cannot be bypassed by future callers that add new
code paths to this file.

The fold runs on cache miss via
`_fold_continuity_events` (in `fold.py`).

Why R2 (composition over mixins)? The
``Result`` chain via ``.bind`` keeps each
``record_*`` flat and the failure path explicit.
The previous monolithic implementation embedded the
PII check inline (with a ``try/except`` or a
``.startswith`` guard); the split makes the gate
un-bypassable because the entity recorder is the
only place that constructs a
``continuity.entity_seen`` event.
"""

from __future__ import annotations

from typing import Callable, Optional

from ...core.event import CorrelationContext, Event, correlation_middleware
from ...core.result import Err, Ok, PersistenceError, Result
from ...infra.hashing import short_hash
from ...infra.redis._errors import MemoryDecodeError, MemoryMiss
from ...infra.redis._memory import ShortMemoryStorage
from ...stream.event_log import EventLog
from ..base import BaseShortTermMemory
from .cache_codec import read_cache, serialize_for_cache
from .fold import _fold_continuity_events
from .pii import PII_HASH_PREFIX
from .recorders import (
    build_category_chosen_event,
    build_entity_seen_event,
    build_tool_used_event,
)
from .state import (
    CONTINUITY_KEY_PREFIX,
    ContinuityEventType,
    ContinuityState,
)


class ContinuityManager(BaseShortTermMemory[ContinuityState]):
    """
    Manages continuity state per (tenant, user). EventLog is
    truth; Redis Hash with sliding TTL is cache.

    Inherits cache orchestration from
    ``BaseShortTermMemory`` and provides the
    continuity-specific shape: Hash-encoded cache, two-part
    identity (``tenant_id``, ``user_id``), and the
    continuity event vocabulary.
    """

    key_prefix = CONTINUITY_KEY_PREFIX

    def __init__(
        self,
        event_log: EventLog,
        storage: ShortMemoryStorage,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        # ``None`` → operator-configured default from
        # ``Settings.continuity_ttl_seconds`` (90 days,
        # sliding — renewed on every ``record_*`` write).
        if ttl_seconds is None:
            from ...infra.config import fresh_settings

            ttl_seconds = fresh_settings().continuity_ttl_seconds
        super().__init__(event_log, storage, ttl_seconds=ttl_seconds)

    # ------------------------------------------------------------------ id

    # Single source of truth for the agent_id convention.
    # The Consolidator's parser consults this attribute to
    # classify an EventLog agent_id. Renaming this value
    # alone is enough to change the wire format.
    agent_id_prefix = "continuity:"

    @classmethod
    def cache_key(cls, tenant_id: str, user_id: str) -> str:
        """Redis key for a single continuity's Hash cache entry."""
        return f"{CONTINUITY_KEY_PREFIX}{tenant_id}:{user_id}"

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def hash_value(value: str) -> str:
        """
        Return the truncated sha256 of a value, used as
        ``value_hash`` for ``continuity.entity_seen`` (ADR-014
        §2.7). Same algorithm as ``(:Problem).fingerprint`` in
        ADR-010 §3.

        The PII gate relies on callers ALWAYS storing
        ``hash_value(v)`` instead of ``v`` in the EventLog
        payload of ``continuity.entity_seen``.
        """
        return PII_HASH_PREFIX + short_hash(value)

    # ------------------------------------------------------------------ public

    async def write_cache(
        self,
        tenant_id: str,
        user_id: str,
        state: ContinuityState,
    ) -> None:
        """
        Write the given ``ContinuityState`` to the Redis Hash
        cache. Replaces any existing value; sets the TTL
        configured on the manager (if any).

        Public API: the ``Projector`` calls this with a state
        it folded from the EventLog.
        """
        key = self.cache_key(tenant_id, user_id)
        await self._write_cache_for_key(key, state)

    async def refresh_cache(self, tenant_id: str, user_id: str) -> None:
        """
        Rebuild the cache for one continuity by folding the
        EventLog. Idempotent.

        Public API: the ``CacheWarmer`` adapter calls this.
        """
        await super().refresh_cache(tenant_id, user_id)

    async def recency_suggest(
        self,
        tenant_id: str,
        user_id: str,
        slot: str,
    ) -> Optional[str]:
        """
        Return the last chosen value for a categorical slot
        (e.g. ``"cfop"``), or ``None`` if there is no record
        or the continuity has been cleared.

        This is the primary read API for agents that want to
        pre-fill inputs with the user's most recent choice.
        Respects ``cleared_at`` (LGPD).
        """
        state = await self.read(tenant_id, user_id)
        if state is None or state.is_cleared():
            return None
        return state.last_categories.get(slot)

    # ------------------------------------------------------------------ write (domain)

    async def _emit_and_refresh(
        self,
        event: Event,
        tenant_id: str,
        user_id: str,
    ) -> Result[Event, PersistenceError]:
        """Append an event and refresh the cache. Idempotent."""
        result = await self._log.append(event)
        if result.is_err():
            err = result.err_value() or PersistenceError("Unknown persistence error")
            return Err(err)
        await self.refresh_cache(tenant_id, user_id)
        return Ok(event)

    async def _build_and_emit(
        self,
        *,
        tenant_id: str,
        user_id: str,
        build: "Callable[[str, CorrelationContext], Result[Event, PersistenceError]]",
    ) -> Result[Event, PersistenceError]:
        """
        Build an event with the recorder and emit it.
        Railway composition: the recorder returns
        ``Result[Event, PersistenceError]``; we bind
        `_emit_and_refresh` to its ``Ok`` branch and
        propagate the ``Err`` untouched.

        The build callback receives the agent_id and the
        current correlation context. Wrapping the
        correlation lookup here keeps the public
        `record_*` methods short — each one is a
        validation block followed by a single
        `_build_and_emit` call.
        """
        agent_id = self.agent_id_for(tenant_id, user_id)
        ctx = correlation_middleware.current()
        build_result = build(agent_id, ctx)
        if build_result.is_err():
            return Err(build_result.err_value())  # type: ignore[arg-type]
        event = build_result.ok_value()  # type: ignore[union-attr]
        return await self._emit_and_refresh(event, tenant_id, user_id)

    async def create(
        self,
        tenant_id: str,
        user_id: str,
    ) -> Result[Event, PersistenceError]:
        """
        Create a continuity record. Idempotent on
        (``tenant_id``, ``user_id``): a second call with the
        same identity is a no-op (the EventLog dedupes on
        event_id).
        """
        agent_id = self.agent_id_for(tenant_id, user_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=ContinuityEventType.CREATED,
            data={
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, tenant_id, user_id)

    async def record_tool_used(
        self,
        tenant_id: str,
        user_id: str,
        tool: str,
        params_fingerprint: str,
        result_signature: str,
        latency_ms: int,
    ) -> Result[Event, PersistenceError]:
        """
        Record that a tool call completed for this user.

        ``params_fingerprint`` MUST be a hash of the params,
        not the params themselves (ADR-014 §2.4).
        ``result_signature`` MUST be a hash of the result,
        not the raw result.
        """
        if not tool:
            return Err(PersistenceError("Empty tool name"))
        return await self._build_and_emit(
            tenant_id=tenant_id,
            user_id=user_id,
            build=lambda agent_id, ctx: build_tool_used_event(
                agent_id=agent_id,
                correlation=ctx,
                tool=tool,
                params_fingerprint=params_fingerprint,
                result_signature=result_signature,
                latency_ms=latency_ms,
            ),
        )

    async def record_entity_seen(
        self,
        tenant_id: str,
        user_id: str,
        kind: str,
        value_hash: str,
        source: str,
    ) -> Result[Event, PersistenceError]:
        """
        Record that an entity was observed.

        ``value_hash`` MUST already be a hash (use
        ``ContinuityManager.hash_value`` before calling). The
        raw value is never accepted here. This is the PII
        gate (ADR-014 §2.7) — it lives inside
        ``build_entity_seen_event`` (via
        ``pii.check_pii_hash``) so no future caller can
        forget it.
        """
        if not kind:
            return Err(PersistenceError("Empty entity kind"))
        if not value_hash:
            return Err(PersistenceError("Empty entity value_hash"))
        return await self._build_and_emit(
            tenant_id=tenant_id,
            user_id=user_id,
            build=lambda agent_id, ctx: build_entity_seen_event(
                agent_id=agent_id,
                correlation=ctx,
                kind=kind,
                value_hash=value_hash,
                source=source,
            ),
        )

    async def record_category_chosen(
        self,
        tenant_id: str,
        user_id: str,
        slot: str,
        value: str,
    ) -> Result[Event, PersistenceError]:
        """
        Record a categorical choice (CFOP, cost center, …).
        """
        if not slot:
            return Err(PersistenceError("Empty category slot"))
        if not value:
            return Err(PersistenceError("Empty category value"))
        return await self._build_and_emit(
            tenant_id=tenant_id,
            user_id=user_id,
            build=lambda agent_id, ctx: build_category_chosen_event(
                agent_id=agent_id,
                correlation=ctx,
                slot=slot,
                value=value,
            ),
        )

    async def clear(
        self,
        tenant_id: str,
        user_id: str,
        reason: str = "user_request",
    ) -> Result[Event, PersistenceError]:
        """
        Erase the continuity state (LGPD right-to-erasure).
        Terminal event. After this, ``read`` returns a state
        with all dicts empty and ``cleared_at`` set, until
        the next ``tool_used``.

        Idempotent on (``tenant_id``, ``user_id``, ``reason``).
        """
        agent_id = self.agent_id_for(tenant_id, user_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=ContinuityEventType.CLEARED,
            data={"reason": reason},
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, tenant_id, user_id)

    # ------------------------------------------------------------------ read

    async def read(self, tenant_id: str, user_id: str) -> Optional[ContinuityState]:
        """Read the continuity state. Cache first, fold on miss."""
        return await super().read(tenant_id, user_id)

    async def list_for_tenant(
        self, tenant_id: str, limit: int = 100
    ) -> list[ContinuityState]:
        """
        Best-effort: scans the Redis cache for continuity
        records belonging to a tenant.
        """
        out: list[ContinuityState] = []
        prefix = f"{CONTINUITY_KEY_PREFIX}{tenant_id}:"
        async for key in self._storage.iter_keys(prefix):
            user_id = key[len(prefix) :]
            state = await self._read_cache(self.cache_key(tenant_id, user_id))
            if state is not None:
                out.append(state)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ base hooks (cache)

    async def _read_cache(
        self, key: str
    ) -> Result[Optional[ContinuityState], MemoryDecodeError]:
        """Decode the Hash cache entry at ``key``.

        Returns ``Ok(None)`` on miss (``MemoryMiss`` from
        the storage) or empty hash; ``Err(MemoryDecodeError)``
        on a real decode error. Hash present but missing
        ``created_at`` is a legacy malformed entry — fold
        fallback is the safe path.
        """
        result = await self._storage.get_record(key)
        if result.is_err():
            err = result.err_value()
            if isinstance(err, MemoryMiss):
                return Ok(None)
            return Err(MemoryDecodeError(f"storage error: {err}", key=key))
        raw = result.ok_value()
        if raw is None:
            return Ok(None)
        decoded = read_cache(raw)
        if decoded is None:
            # ``read_cache`` returns ``None`` when the hash
            # is present but missing ``created_at`` (a
            # malformed legacy entry). Fold-fallback is
            # the safe path, so we surface a typed decode
            # error and let the base class do the right
            # thing.
            return Err(MemoryDecodeError("cache_codec rejected the payload", key=key))
        return Ok(decoded)

    def _serialize_for_cache(self, state: ContinuityState) -> dict[str, str]:
        """Encode a ContinuityState to a Hash mapping for ``HSET``."""
        return serialize_for_cache(state)

    def _store_cache(self, key: str, payload, ttl) -> None:
        """Deprecated hook — the storage layer handles writes now."""
        return None

    # ------------------------------------------------------------------ base hooks (fold)

    async def _fold_from_log(
        self, tenant_id: str, user_id: str
    ) -> Optional[ContinuityState]:
        agent_id = self.agent_id_for(tenant_id, user_id)
        events = await self._log.read(agent_id)
        return _fold_continuity_events(tenant_id, user_id, events)


__all__ = ["ContinuityManager"]
