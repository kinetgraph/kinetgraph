# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Session — short-term conversational memory.

A session is modeled as an **agent** in the EventLog. Its
agent_id is `"session:{session_id}"`. Its events have
`event_class="domain"` and follow the session vocabulary:

  - "session.started"     : the session was opened
                            data: { user_id, tenant_id, ... }
  - "session.message"     : a message in the session
                            data: { role: "user"|"assistant", content, ... }
  - "session.context"     : context mutation (e.g. scratchpad)
                            data: { key, value }
  - "session.ended"       : the session was closed (terminal)

The "state" of a session is a flat dict[slot, value] derived
from the last `session.context` events and the accumulated
messages. The default projection in `core.world` stores the
last domain event's data as a single component; for richer
semantics the application can supply a custom projection
that aggregates messages and context keys.

The cache at `knt:session:{id}` is a **TTL cache** of the
folded state, with a default TTL of 24h. It is rebuilt on
demand from the EventLog when the TTL expires.

Iteration 2 (ADR-019): the cache is owned by the
``MemoryStorage`` Protocol; the manager consumes the
concrete ``RedisSessionStorage`` via constructor injection.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

from ..core._typing import JsonValue
from ..core.event import Event, correlation_middleware
from ..core.result import Err, Ok, PersistenceError, Result
from ..infra.redis._errors import MemoryDecodeError, MemoryMiss
from ..infra.redis._memory import ShortMemoryStorage
from ..stream.event_log import EventLog
from .base import BaseShortTermMemory

import structlog

logger = structlog.get_logger()


SESSION_KEY_PREFIX = "knt:session:"

# Backwards-compat re-export. The default TTL now lives
# in ``Settings.session_ttl_seconds`` (see
# ``kntgraph.infra.config``); this constant is kept
# so downstream code that imported it keeps working.
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # == Settings.session_ttl_seconds default
MAX_MESSAGES_IN_CACHE = 100


class SessionEventType:
    STARTED = "session.started"
    MESSAGE = "session.message"
    CONTEXT = "session.context"
    ENDED = "session.ended"


@dataclass(frozen=True, slots=True)
class SessionState:
    """
    Cached projection of a session. Derived from the event
    stream but stored in Redis JSON for fast read access.
    """

    session_id: str
    user_id: str
    tenant_id: str
    messages: tuple[dict[str, JsonValue], ...]
    context: dict[str, JsonValue]
    started_at: float
    ended_at: Optional[float] = None

    def is_active(self) -> bool:
        return self.ended_at is None


class SessionManager(BaseShortTermMemory[SessionState]):
    """
    Manages a single session (or many, by session_id).

    Inherits the cache orchestration from
    ``BaseShortTermMemory`` and provides the session-specific
    shape: JSON-encoded cache, single-part identity
    (``session_id``), and the session event vocabulary.
    """

    key_prefix = SESSION_KEY_PREFIX

    def __init__(
        self,
        event_log: EventLog,
        storage: ShortMemoryStorage,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        # ``None`` → operator-configured default from
        # ``Settings.session_ttl_seconds`` (24h). The
        # base class interprets ``None`` as "no TTL";
        # we resolve it here so the memory contract is
        # uniform (an integer always lands at the base).
        if ttl_seconds is None:
            from ..infra.config import fresh_settings

            ttl_seconds = fresh_settings().session_ttl_seconds
        super().__init__(event_log, storage, ttl_seconds=ttl_seconds)

    # ------------------------------------------------------------------ id

    # Single source of truth for the agent_id convention.
    # The Consolidator's parser consults this attribute to
    # classify an EventLog agent_id. Renaming this value
    # alone is enough to change the wire format.
    agent_id_prefix = "session:"

    @classmethod
    def cache_key(cls, session_id: str) -> str:
        """Redis key for a single session's cache entry."""
        return SESSION_KEY_PREFIX + session_id

    # ------------------------------------------------------------------ public

    async def write_cache(self, session_id: str, state: SessionState) -> None:
        """
        Public write-through for a single session.

        Public API: the ``Projector`` calls this with a state
        it folded from the EventLog (see
        ``kntgraph.memory.consolidation.Projector``).

        `start` also calls this for the brand-new initial
        state, which has no EventLog history yet.
        """
        key = self.cache_key(session_id)
        await self._write_cache_for_key(key, state)

    async def refresh_cache(self, session_id: str) -> None:
        """
        Rebuild the cache for one session by folding the
        EventLog. Idempotent: if no events exist, this is a
        no-op.

        Public API: the ``CacheWarmer`` adapter calls this in
        response to a ``CacheRefreshRequest`` (see
        ``kntgraph.memory.cache_warmer``).
        """
        await super().refresh_cache(session_id)

    # ------------------------------------------------------------------ write (domain)

    async def _emit_and_refresh(
        self,
        event: Event,
        session_id: str,
    ) -> Result[Event, PersistenceError]:
        """
        Append an event to the EventLog; on success, refresh
        the cache and return Ok(event). On failure, propagate
        the error without touching the cache.
        """
        result = await self._log.append(event)
        if result.is_err():
            err = result.err_value() or PersistenceError("Unknown persistence error")
            return Err(err)
        await self.refresh_cache(session_id)
        return Ok(event)

    async def start(
        self,
        session_id: str,
        user_id: str,
        tenant_id: str,
        metadata: Optional[dict] = None,
    ) -> Result[Event, PersistenceError]:
        """
        Open a new session. Idempotent on (session_id, user_id,
        tenant_id): a second call with the same id is a no-op
        (the EventLog is idempotent on event_id).

        The `metadata` argument seeds the session context.
        It is recorded as one `session.context` event per
        key, NOT as part of the `session.started` payload.
        """
        agent_id = self.agent_id_for(session_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=SessionEventType.STARTED,
            data={
                "session_id": session_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
            },
            correlation=ctx,
        )
        result = await self._log.append(e)
        if result.is_err():
            err = result.err_value() or PersistenceError("Unknown persistence error")
            return Err(err)
        for key, value in (metadata or {}).items():
            ctx_event = Event.domain_from(
                agent_id=agent_id,
                type=SessionEventType.CONTEXT,
                data={"key": str(key), "value": value},
                correlation=ctx,
            )
            ctx_result = await self._log.append(ctx_event)
            if ctx_result.is_err():
                err = ctx_result.err_value() or PersistenceError(
                    "Unknown persistence error"
                )
                return Err(err)
        await self.write_cache(
            session_id,
            SessionState(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                messages=(),
                context=dict(metadata or {}),
                started_at=e.timestamp.timestamp(),
            ),
        )
        return Ok(e)

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> Result[Event, PersistenceError]:
        """Append a message to the session."""
        if not content:
            return Err(PersistenceError("Empty message content"))
        agent_id = self.agent_id_for(session_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=SessionEventType.MESSAGE,
            data={
                "role": role,
                "content": content,
                "metadata": dict(metadata or {}),
            },
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, session_id)

    async def set_context(
        self,
        session_id: str,
        key: str,
        value: JsonValue,
    ) -> Result[Event, PersistenceError]:
        """
        Mutate the session context. Key/value storage; later
        writes with the same key override.
        """
        agent_id = self.agent_id_for(session_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=SessionEventType.CONTEXT,
            data={"key": key, "value": value},
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, session_id)

    async def end(
        self,
        session_id: str,
    ) -> Result[Event, PersistenceError]:
        """Close the session. Terminal event."""
        agent_id = self.agent_id_for(session_id)
        ctx = correlation_middleware.current()
        e = Event.domain_from(
            agent_id=agent_id,
            type=SessionEventType.ENDED,
            data={},
            correlation=ctx,
        )
        return await self._emit_and_refresh(e, session_id)

    # ------------------------------------------------------------------ read

    async def read(self, session_id: str) -> Optional[SessionState]:
        """
        Read the session state. Tries the cache first; on miss,
        folds the EventLog and refreshes the cache.
        """
        return await super().read(session_id)

    async def list_active(self, tenant_id: str, limit: int = 100) -> list[SessionState]:
        """
        Best-effort: scans all session keys in Redis. If the
        cache is cold, returns whatever is in Redis. Callers
        that need a complete picture should fold the EventLog.
        """
        out: list[SessionState] = []
        async for key in self._storage.iter_keys(SESSION_KEY_PREFIX):
            sid = key[len(SESSION_KEY_PREFIX) :]
            state = await self._read_cache(self.cache_key(sid))
            if state and state.tenant_id == tenant_id and state.is_active():
                out.append(state)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ base hooks (cache)

    async def _read_cache(
        self, key: str
    ) -> Result[Optional[SessionState], MemoryDecodeError]:
        """Decode the JSON cache entry at ``key``.

        Returns ``Ok(None)`` on miss (``MemoryMiss`` from
        the storage); ``Err(MemoryDecodeError)`` on a
        malformed payload. The base class treats ``Err``
        as a cache miss (logs at WARNING) so a transient
        Redis blip does not block the read-through.
        """
        result = await self._storage.get_record(key)
        if result.is_err():
            err = result.err_value()
            if isinstance(err, MemoryMiss):
                return Ok(None)
            # Propagate the typed error — base will log.
            return Err(MemoryDecodeError(f"storage error: {err}", key=key))
        raw = result.ok_value()
        if raw is None:
            return Ok(None)
        try:
            d = dict(raw)
            return Ok(
                SessionState(
                    session_id=d["session_id"],
                    user_id=d["user_id"],
                    tenant_id=d["tenant_id"],
                    messages=tuple(d.get("messages", [])),
                    context=dict(d.get("context", {})),
                    started_at=float(d.get("started_at", 0.0)),
                    ended_at=(
                        float(d["ended_at"]) if d.get("ended_at") is not None else None
                    ),
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            return Err(MemoryDecodeError(f"invalid session payload: {e}", key=key))

    def _serialize_for_cache(self, state: SessionState) -> dict:
        """Encode a SessionState to a JSON-compatible dict."""
        return {
            "session_id": state.session_id,
            "user_id": state.user_id,
            "tenant_id": state.tenant_id,
            "messages": list(state.messages),
            "context": state.context,
            "started_at": state.started_at,
            "ended_at": state.ended_at,
        }

    def _store_cache(self, key: str, payload, ttl) -> None:
        """Deprecated hook — the storage layer handles writes now."""
        # Kept as a no-op for back-compat with subclasses
        # that may still call it. Iteration 2 routes writes
        # through ``self._storage.put_record`` directly.
        return None

    # ------------------------------------------------------------------ base hooks (fold)

    async def _fold_from_log(self, session_id: str) -> Optional[SessionState]:
        """
        Folds the EventLog for the session and reconstructs a
        SessionState. Pure: reads events, no side effects.
        """
        agent_id = self.agent_id_for(session_id)
        events = await self._log.read(agent_id)
        return _fold_session_events(session_id, events)


def _fold_session_events(
    session_id: str,
    events: Iterable[Event],
) -> Optional[SessionState]:
    """
    Pure fold: events → SessionState.

    Returns None if no `session.started` event was found.

    `started_at` and `ended_at` come from the Event's
    `timestamp` (not from `data`, which is now
    idempotency-stable).
    """
    user_id = ""
    tenant_id = ""
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    messages: list[dict[str, JsonValue]] = []
    context: dict[str, JsonValue] = {}

    for e in events:
        if e.event_type == SessionEventType.STARTED:
            user_id = e.data.get("user_id", "")
            tenant_id = e.data.get("tenant_id", "")
            started_at = e.timestamp.timestamp()
            context.update(e.data.get("metadata", {}))
        elif e.event_type == SessionEventType.MESSAGE:
            messages.append(
                {
                    "role": e.data.get("role", "user"),
                    "content": e.data.get("content", ""),
                    "metadata": e.data.get("metadata", {}),
                    "at": e.timestamp.timestamp(),
                }
            )
            if len(messages) > MAX_MESSAGES_IN_CACHE:
                # Keep only the most recent MAX_MESSAGES_IN_CACHE
                # in cache; the EventLog retains everything.
                messages[:-MAX_MESSAGES_IN_CACHE] = []
        elif e.event_type == SessionEventType.CONTEXT:
            key = e.data.get("key")
            if key is not None:
                context[key] = e.data.get("value")
        elif e.event_type == SessionEventType.ENDED:
            ended_at = e.timestamp.timestamp()

    if started_at is None:
        return None

    return SessionState(
        session_id=session_id,
        user_id=user_id,
        tenant_id=tenant_id,
        messages=tuple(messages),
        context=context,
        started_at=started_at,
        ended_at=ended_at,
    )
