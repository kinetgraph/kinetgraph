# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Profile — long-term **static** preferences of the PME / user.

A profile is modeled as an **agent** in the EventLog. Its
agent_id is `"profile:{tenant_id}:{user_id}"`. Its events
have `event_class="domain"` and follow the profile vocabulary:

  - "profile.created"          : first sighting of the profile
                                data: { preferences: {key: value}, ... }
  - "profile.preference_set"   : set a single key/value
                                data: { key, value }
  - "profile.preference_unset" : remove a key
                                data: { key }
  - "profile.tier_changed"     : SLA tier changed
                                data: { to_tier }

The state is a flat dict[str, str]. The current state is
derived by fold: for each `preference_set`, the value
overrides; for `preference_unset`, the key is removed.

The Redis Hash at `knt:profile:{tenant_id}:{user_id}` is a
**cache** (no TTL by default — profiles are long-lived). On
miss, the cache is rebuilt from the EventLog.

Cache format: Redis Hash (``HGETALL``/``HSET`` + ``DEL``). For
a JSON-based cache see ``SessionManager``.

**Separação com `continuity`** (ADR-014): `profile` modela
"o que a PME é" — config estável (regime tributário, tier
SLA, e-mail de NF-e, idioma). Estado-de-uso recente
(última tool, último cliente, último CFOP) pertence ao tier
`continuity` (`memory/continuity.py`), que tem TTL sliding,
PII hash-only e suporte a LGPD `cleared`. Se um campo
muda em resposta a uma tool call → `continuity`. Se muda
por configuração explícita ou billing → `profile`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from ..core._typing import JsonValue
from ..core.event import Event, correlation_middleware
from ..core.result import Err, Ok, PersistenceError, Result
from ..infra.redis._errors import MemoryDecodeError, MemoryMiss
from ..infra.redis._memory import ShortMemoryStorage
from ..stream.event_log import EventLog
from .base import BaseShortTermMemory


logger = structlog.get_logger()


PROFILE_KEY_PREFIX = "knt:profile:"

# Backwards-compat re-export. The default TTL now lives
# in ``Settings.profile_ttl_seconds`` (None = no TTL);
# this constant is kept for downstream code that
# imported it.
DEFAULT_TTL_SECONDS: Optional[int] = None


class ProfileEventType:
    CREATED = "profile.created"
    PREFERENCE_SET = "profile.preference_set"
    PREFERENCE_UNSET = "profile.preference_unset"
    TIER_CHANGED = "profile.tier_changed"


@dataclass(frozen=True, slots=True)
class ProfileState:
    """
    Cached projection of a profile. Flat key/value + tier
    field for fast lookup.
    """

    tenant_id: str
    user_id: str
    preferences: dict[str, str]
    tier: str  # "vip", "standard", "basic"
    created_at: float
    updated_at: float


class ProfileManager(BaseShortTermMemory[ProfileState]):
    """
    Manages profiles. EventLog is truth; Redis Hash is cache.

    Inherits the cache orchestration from
    ``BaseShortTermMemory`` and provides the profile-specific
    shape: Hash-encoded cache, two-part identity
    (``tenant_id``, ``user_id``), and the profile event
    vocabulary.
    """

    key_prefix = PROFILE_KEY_PREFIX

    def __init__(
        self,
        event_log: EventLog,
        storage: ShortMemoryStorage,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        # ``None`` → operator-configured default from
        # ``Settings.profile_ttl_seconds`` (None = no TTL
        # by default; profile is the stable PME config
        # that outlives sessions).
        if ttl_seconds is None:
            from ..infra.config import fresh_settings

            ttl_seconds = fresh_settings().profile_ttl_seconds
        super().__init__(event_log, storage, ttl_seconds=ttl_seconds)

    # ------------------------------------------------------------------ id

    # Single source of truth for the agent_id convention.
    # The Consolidator's parser consults this attribute to
    # classify an EventLog agent_id. Renaming this value
    # alone is enough to change the wire format.
    agent_id_prefix = "profile:"

    @classmethod
    def cache_key(  # type: ignore[reportIncompatibleMethodOverride]
        cls, tenant_id: str, user_id: str
    ) -> str:
        """Redis key for a single profile's Hash cache entry."""
        return f"{PROFILE_KEY_PREFIX}{tenant_id}:{user_id}"

    # ------------------------------------------------------------------ public

    async def write_cache(
        self,
        tenant_id: str,
        user_id: str,
        state: ProfileState,
    ) -> None:
        """
        Write the given ``ProfileState`` to the Redis Hash
        cache. Replaces any existing value; sets the TTL
        configured on the manager (if any).

        Public API: the ``Projector`` calls this with a state
        it folded from the EventLog.
        """
        key = self.cache_key(tenant_id, user_id)
        await self._write_cache_for_key(key, state)

    async def refresh_cache(  # type: ignore[reportIncompatibleMethodOverride]
        self, tenant_id: str, user_id: str
    ) -> None:
        """
        Rebuild the cache for one profile by folding the
        EventLog. Idempotent.

        Public API: the ``CacheWarmer`` adapter calls this.
        """
        await super().refresh_cache(tenant_id, user_id)

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

    async def create(
        self,
        tenant_id: str,
        user_id: str,
        preferences: Optional[dict[str, str]] = None,
        tier: str = "standard",
    ) -> Result[Event, PersistenceError]:
        """
        Create a profile. Idempotent (the EventLog dedupes
        on event_id, which is a function of the deterministic
        data payload below — no wall-clock in `data`).
        """
        agent_id = self.agent_id_for(tenant_id, user_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=ProfileEventType.CREATED,
            data={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "preferences": dict(preferences or {}),
                "tier": tier,
            },
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, tenant_id, user_id)

    async def set_preference(
        self,
        tenant_id: str,
        user_id: str,
        key: str,
        value: str,
    ) -> Result[Event, PersistenceError]:
        """Set a single preference (key/value)."""
        agent_id = self.agent_id_for(tenant_id, user_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=ProfileEventType.PREFERENCE_SET,
            data={"key": key, "value": value},
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, tenant_id, user_id)

    async def unset_preference(
        self,
        tenant_id: str,
        user_id: str,
        key: str,
    ) -> Result[Event, PersistenceError]:
        """Remove a preference key."""
        agent_id = self.agent_id_for(tenant_id, user_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=ProfileEventType.PREFERENCE_UNSET,
            data={"key": key},
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, tenant_id, user_id)

    async def change_tier(
        self,
        tenant_id: str,
        user_id: str,
        to_tier: str,
    ) -> Result[Event, PersistenceError]:
        """
        Change the SLA tier of the user.

        Idempotent on (tenant_id, user_id, to_tier): repeated
        calls with the same target produce the same event_id
        and collapse to a single append in the EventLog.
        The previous tier is NOT recorded in `data` because it
        is derived state (computed by the fold from prior
        events).
        """
        agent_id = self.agent_id_for(tenant_id, user_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=ProfileEventType.TIER_CHANGED,
            data={"to_tier": to_tier},
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, tenant_id, user_id)

    # ------------------------------------------------------------------ read

    async def read(  # type: ignore[reportIncompatibleMethodOverride]
        self, tenant_id: str, user_id: str
    ) -> Optional[ProfileState]:
        """Read the profile. Cache first, fold on miss."""
        return await super().read(tenant_id, user_id)

    async def list_for_tenant(
        self, tenant_id: str, limit: int = 100
    ) -> list[ProfileState]:
        """
        Best-effort: scans the Redis cache for profiles
        belonging to a tenant.
        """
        out: list[ProfileState] = []
        prefix = f"{PROFILE_KEY_PREFIX}{tenant_id}:"
        async for key in self._storage.iter_keys(prefix):
            decoded_user_id = key[len(prefix) :]
            cache_result = await self._read_cache(
                self.cache_key(tenant_id, decoded_user_id),
                tenant_id,
                decoded_user_id,
            )
            if cache_result.is_err():
                continue
            state = cache_result.ok_value()
            if state is not None:
                out.append(state)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ base hooks (cache)

    async def _read_cache(
        self, key: str, *key_parts: str
    ) -> Result[Optional[ProfileState], MemoryDecodeError]:
        """Decode the Hash cache entry at ``key``.

        Returns ``Ok(None)`` on miss (``MemoryMiss`` from
        the storage); ``Err(MemoryDecodeError)`` on a
        malformed payload or Redis-side failure (other
        storage errors are re-typed as ``MemoryDecodeError``
        for the caller — the base class treats them as a
        cache miss to keep the read-through path alive).

        The wire payload from the ``ShortMemoryStorage``
        Protocol is ``Mapping[str, JsonValue]``. We
        coerce each slot to the expected scalar
        (``str``/``float``) via the local
        :func:`_coerce_profile_scalar` helper so the
        state dataclass accepts the values.
        """
        result = await self._storage.get_record(key)
        if result.is_err():
            err = result.err_value()
            if isinstance(err, MemoryMiss):
                return Ok(None)
            return Err(MemoryDecodeError(f"storage error: {err}", key=key))
        decoded = result.ok_value()
        if not decoded:
            return Ok(None)
        if "created_at" not in decoded:
            return Err(MemoryDecodeError("missing required field: created_at", key=key))
        # ``tenant_id`` and ``user_id`` are encoded in the
        # Redis key itself, not in the Hash payload. The
        # base passes the original ``key_parts`` so we can
        # reconstruct the identity.
        tenant_id = key_parts[0] if len(key_parts) >= 1 else ""
        user_id = key_parts[1] if len(key_parts) >= 2 else ""
        return Ok(
            _build_profile_state(
                decoded,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        )

    def _serialize_for_cache(self, state: ProfileState) -> dict[str, str]:
        """Encode a ProfileState to a Hash mapping for ``HSET``."""
        mapping: dict[str, str] = {
            "tier": state.tier,
            "created_at": str(state.created_at),
            "updated_at": str(state.updated_at),
        }
        for k, v in state.preferences.items():
            mapping[f"pref:{k}"] = str(v)
        return mapping

    def _store_cache(
        self,
        key: str,
        payload: object,
        ttl: Optional[int],
    ) -> None:
        """Deprecated hook — the storage layer handles writes now."""
        return None

    # ------------------------------------------------------------------ base hooks (fold)

    async def _fold_from_log(  # type: ignore[reportIncompatibleMethodOverride]
        self, tenant_id: str, user_id: str
    ) -> Optional[ProfileState]:
        agent_id = self.agent_id_for(tenant_id, user_id)
        events = await self._log.read(agent_id)
        return _fold_profile_events(tenant_id, user_id, events)


def _fold_profile_events(
    tenant_id: str,
    user_id: str,
    events: Iterable[Event],
) -> Optional[ProfileState]:
    """
    Pure fold of profile events. Returns None if no
    `profile.created` event is present.

    `created_at` and `updated_at` come from the Event's
    `timestamp` (not from `data`, which is now
    idempotency-stable).

    Per-event dispatch is delegated to a small table of
    handlers (``_PROFILE_HANDLERS``) below — the fold
    itself stays a linear loop and stays under the CC ≤ 10
    ceiling (each handler is ≤ 5).
    """
    state: dict[str, Any] = {
        "created_at": None,
        "updated_at": 0.0,
        "preferences": {},
        "tier": "standard",
    }

    for e in events:
        handler = _PROFILE_HANDLERS.get(e.event_type)
        if handler is not None:
            handler(e, state)

    if state["created_at"] is None:
        return None

    return ProfileState(
        tenant_id=tenant_id,
        user_id=user_id,
        preferences=state["preferences"],
        tier=state["tier"],
        created_at=state["created_at"],
        updated_at=state["updated_at"],
    )


def _coerce_profile_scalar_value(
    value: Any,
    *,
    fallback: str,
) -> str:
    """Coerce a profile scalar slot to ``str``.

    Returns ``fallback`` for non-string scalar values
    that are not coercible (dict / list). The check is
    the same one the fold used inline: ``str(v)`` for
    primitives, ``fallback`` for containers.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return fallback
    return str(value)


def _on_profile_created(e: Event, state: dict[str, Any]) -> None:
    """``profile.created`` handler: initialise the state
    and seed preferences / tier from the event payload."""
    ts = e.timestamp.timestamp()
    state["created_at"] = ts
    state["updated_at"] = ts
    prefs = e.data.get("preferences")
    if isinstance(prefs, dict):
        for k, v in prefs.items():
            if isinstance(k, str):
                state["preferences"][k] = _coerce_profile_scalar_value(v, fallback="")
    state["tier"] = _coerce_profile_scalar_value(
        e.data.get("tier", state["tier"]), fallback=state["tier"]
    )


def _on_profile_preference_set(e: Event, state: dict[str, Any]) -> None:
    """``profile.preference_set`` handler: write a key."""
    k = e.data.get("key")
    if isinstance(k, str):
        state["preferences"][k] = _coerce_profile_scalar_value(
            e.data.get("value"), fallback=""
        )
    state["updated_at"] = e.timestamp.timestamp()


def _on_profile_preference_unset(e: Event, state: dict[str, Any]) -> None:
    """``profile.preference_unset`` handler: drop a key."""
    k = e.data.get("key")
    if isinstance(k, str):
        state["preferences"].pop(k, None)
    state["updated_at"] = e.timestamp.timestamp()


def _on_profile_tier_changed(e: Event, state: dict[str, Any]) -> None:
    """``profile.tier_changed`` handler: update the tier."""
    state["tier"] = _coerce_profile_scalar_value(
        e.data.get("to_tier"), fallback=state["tier"]
    )
    state["updated_at"] = e.timestamp.timestamp()


_ProfileHandler = Callable[[Event, dict[str, Any]], None]
"""Per-event-type side-effect on the fold's ``state``
dict. Each handler reads the event payload and mutates
``state`` in place; the fold itself stays a linear
``for`` loop. Keeping the handlers as module-level
functions (rather than nested closures) gives the
dispatch table a stable identity and a clean type
alias for the ``dict[str, _ProfileHandler]`` map.
"""


_PROFILE_HANDLERS: dict[str, _ProfileHandler] = {
    ProfileEventType.CREATED: _on_profile_created,
    ProfileEventType.PREFERENCE_SET: _on_profile_preference_set,
    ProfileEventType.PREFERENCE_UNSET: _on_profile_preference_unset,
    ProfileEventType.TIER_CHANGED: _on_profile_tier_changed,
}


def _coerce_profile_scalar(
    decoded: "Mapping[str, JsonValue]",
    key: str,
    default: str,
) -> str:
    """Coerce a string-valued slot to ``str``. Used by
    :func:`_build_profile_state` so the decoder accepts
    the ``JsonValue`` shape returned by
    ``ShortMemoryStorage``.
    """
    value = decoded.get(key, default)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def _coerce_profile_float(value: JsonValue) -> float:
    """Coerce a ``JsonValue`` to ``float``; returns
    ``0.0`` for non-numeric values.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _build_profile_state(
    decoded: "Mapping[str, JsonValue]",
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> ProfileState:
    """Build a ``ProfileState`` from a Hash payload
    (``Mapping[str, JsonValue]``). The Hash layout uses
    ``pref:<key>`` for preferences; scalar fields are
    ``tier``, ``created_at``, ``updated_at``. The
    identity (``tenant_id``/``user_id``) is encoded in
    the Redis key; the manager passes it explicitly via
    the ``tenant_id``/``user_id`` kwargs so the decoded
    state knows who it belongs to.
    """
    preferences: dict[str, str] = {}
    for k, v in decoded.items():
        if isinstance(k, str) and k.startswith("pref:"):
            preferences[k[len("pref:") :]] = (
                str(v) if not isinstance(v, (dict, list)) else ""
            )
    tier = _coerce_profile_scalar(decoded, "tier", "standard")
    created_at = _coerce_profile_float(decoded.get("created_at"))
    updated_at = _coerce_profile_float(decoded.get("updated_at"))
    return ProfileState(
        tenant_id=tenant_id,
        user_id=user_id,
        preferences=preferences,
        tier=tier,
        created_at=created_at,
        updated_at=updated_at,
    )
