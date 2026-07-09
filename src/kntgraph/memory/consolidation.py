# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Consolidation — bridges between the EventLog (truth) and the
Redis cache (working set).

The consolidation module provides two complementary tools:

  1. `Consolidator` — a *pure* cyclic system that, on each
     tick, scans the World and publishes `CacheRefreshRequest`s
     onto an in-memory bus. The system itself performs NO I/O.

  2. `CacheWarmer` (see `cache_warmer.py`) — a separate
     adapter that subscribes to the bus and applies the
     requests to the Redis cache.

  3. `Projector` — a one-shot fold that takes a `World` and
     projects it to whatever cache structure the application
     needs. Useful for snapshotting or for forcing a cache
     write without an actual Redis miss.

The Consolidation philosophy:

  - The EventLog is always the source of truth.
  - The cache (Hash for profile, JSON for session) is
    derived. It is allowed to be empty or stale.
  - On cache miss, reads reconstruct from the EventLog and
    refresh the cache.
  - The Consolidator is a *housekeeping* tool, not a critical
    path. It exists for operational hygiene.

This module does NOT add new keys, events, or dependencies. It
just orchestrates existing components.

Why a bus, not a method call
----------------------------
Calling `refresh_cache` directly from a `CyclicSystem` would
violate the "systems are pure" contract in `core.system` and
tighten the coupling between the Consolidator and the cache
backend. By going through a bus:

  - The Consolidator is a pure function `(World) -> list[Event]`,
    composable with any other cyclic system.
  - The cache backend is swappable (Redis, in-memory LRU,
    external service) by replacing the `CacheWarmer`
    implementation, without touching the Consolidator.
  - Tests can assert "the Consolidator enqueued N requests
    for agents X, Y, Z" without standing up a Redis client.

The agent_id parser
-------------------
`parse_agent_id` is the SINGLE place that knows the agent_id
string convention. It returns a `MemoryAgent` discriminated
dataclass (frozen, slots) — never a heterogeneous tuple. Call
sites use `match` to narrow without `cast`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

import structlog

from ..core.event import Event
from ..core.world import World
from ..stream.event_log import EventLog
from .cache_warmer import (
    CacheRefreshBus,
    CacheRefreshRequest,
)
from .profile import (
    ProfileManager,
    _fold_profile_events,
)
from .session import (
    SessionManager,
    _fold_session_events,
)

if TYPE_CHECKING:
    from .continuity import ContinuityManager

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# MemoryAgent — the discriminated identity of a memory agent
# ---------------------------------------------------------------------------


# The four discriminators of the memory-tiers' sinks.
# `"session"`, `"profile"` and `"continuity"` are agents in
# the EventLog (consolidated by the in-tick `Consolidator`);
# `"business"` is a sink discriminator for the Solution tier
# (ADR-010), which is NOT an agent and is consolidated by the
# post-tick `KnowledgeConsolidator`. The Literal is exhausted
# in the Consolidator's `refresh_all` and in
# `parse_agent_id`'s `match`. See ADR-010 §2.1 for the
# rationale of the `"business"` name and ADR-014 §2.1 for
# `"continuity"` (estado-de-uso recente, separado de
# `"profile"` para não sobrecarregar preferências estáticas
# com recência).
MemoryKind = Literal["session", "profile", "continuity", "business"]


@dataclass(frozen=True, slots=True)
class MemoryAgent:
    """
    A memory agent in the EventLog. Three flavours:

      - ``MemoryAgent.session(session_id)``
        → agent_id is ``"session:{session_id}"``;
        key parts are ``(session_id,)``.

      - ``MemoryAgent.profile(tenant_id, user_id)``
        → agent_id is ``"profile:{tenant_id}:{user_id}"``;
        key parts are ``(tenant_id, user_id)``.

      - ``MemoryAgent.continuity(tenant_id, user_id)``
        → agent_id is ``"continuity:{tenant_id}:{user_id}"``;
        key parts are ``(tenant_id, user_id)``.
        See ADR-014.

    `id1` and `id2` mirror the CacheRefreshRequest shape
    (the warmer needs to pass them as positional strings).
    `id2` is the empty string for sessions, which the
    warmer can use to detect single-part identities.
    """

    kind: MemoryKind
    id1: str
    id2: str = ""

    @classmethod
    def session(cls, session_id: str) -> "MemoryAgent":
        return cls(kind="session", id1=session_id)

    @classmethod
    def profile(cls, tenant_id: str, user_id: str) -> "MemoryAgent":
        return cls(kind="profile", id1=tenant_id, id2=user_id)

    @classmethod
    def continuity(cls, tenant_id: str, user_id: str) -> "MemoryAgent":
        return cls(kind="continuity", id1=tenant_id, id2=user_id)

    @property
    def agent_id(self) -> str:
        """The full EventLog agent_id for this memory agent."""
        if self.kind == "session":
            return SessionManager.agent_id_for(self.id1)
        if self.kind == "profile":
            return ProfileManager.agent_id_for(self.id1, self.id2)
        # kind == "continuity"
        from .continuity import ContinuityManager

        return ContinuityManager.agent_id_for(self.id1, self.id2)

    @property
    def cache_key(self) -> str:
        """The Redis cache key for this memory agent."""
        if self.kind == "session":
            return SessionManager.cache_key(self.id1)
        if self.kind == "profile":
            return ProfileManager.cache_key(self.id1, self.id2)
        # kind == "continuity"
        from .continuity import ContinuityManager

        return ContinuityManager.cache_key(self.id1, self.id2)

    def __repr__(self) -> str:
        if self.kind == "session":
            return f"MemoryAgent(session, id1={self.id1!r})"
        return f"MemoryAgent({self.kind}, id1={self.id1!r}, id2={self.id2!r})"


# ---------------------------------------------------------------------------
# parse_agent_id — the only place that knows the string convention
# ---------------------------------------------------------------------------


# Registry of (manager_class, kind) pairs. The parser iterates
# this list to find the manager whose `agent_id_prefix` matches
# the start of the agent_id. Adding a new memory type means
# adding ONE entry here — and exposing `agent_id_prefix` on
# the new manager.
#
# ``continuity`` foi adicionado em ADR-014 para separar
# estado-de-uso recente de preferências estáticas (perfil).
# Import local para evitar ciclo com ``continuity.py`` na
# inicialização do módulo.
def _build_manager_registry() -> tuple[tuple[str, type], ...]:
    from .continuity import ContinuityManager

    return (
        ("session", SessionManager),
        ("profile", ProfileManager),
        ("continuity", ContinuityManager),
    )


_MANAGER_REGISTRY: tuple[tuple[str, type], ...] = _build_manager_registry()


def parse_agent_id(agent_id: str) -> Optional[MemoryAgent]:
    """
    Parse an EventLog agent_id into a ``MemoryAgent``.

    Returns ``None`` for agent_ids that are NOT memory
    (e.g. ``"fechamento:..."``, ``"NF-..."``, ``"agent.spawned"``).
    The Consolidator and Projector skip these silently,
    matching the original behaviour.

    The classification is driven by the
    ``agent_id_prefix`` class attribute on each
    memory manager. Renaming a prefix in one place
    updates the parser automatically.

    The body after the prefix MAY contain colons (e.g. a
    session id like ``"tenant-x:user-y:sess-1"``). For
    profiles and continuity, the split is on the FIRST
    colon after the prefix, so ``"profile:tenant-x:user-y:extra"``
    becomes ``MemoryAgent.profile("tenant-x", "user-y:extra")``.
    """
    if not agent_id:
        return None
    for kind, manager_cls in _MANAGER_REGISTRY:
        prefix = manager_cls.agent_id_prefix
        if not agent_id.startswith(prefix):
            continue
        body = agent_id[len(prefix) :]
        if not body:
            return None
        if kind == "session":
            return MemoryAgent.session(body)
        # kind in {"profile", "continuity"}
        if ":" not in body:
            return None
        tenant_id, user_id = body.split(":", 1)
        if not tenant_id or not user_id:
            return None
        if kind == "profile":
            return MemoryAgent.profile(tenant_id, user_id)
        # kind == "continuity"
        return MemoryAgent.continuity(tenant_id, user_id)
    return None


# ---------------------------------------------------------------------------
# Consolidator — pure cyclic system
# ---------------------------------------------------------------------------


class Consolidator:
    """
    Pure cyclic system that publishes `CacheRefreshRequest`s
    for every memory agent in the current World.

    The Consolidator itself performs NO I/O — the actual
    cache writes are handled by `CacheWarmer`, which
    subscribes to the bus and applies the requests.

    Use as a system in the Runner:

        bus = CacheRefreshBus()
        cons = Consolidator(log, bus)
        warmer = CacheWarmer(bus, sm, pm)

        runner = Runner(
            log,
            cyclic_systems=[cons.as_cyclic_system()],
        )

        # In a background task:
        asyncio.create_task(warmer.run_forever())
    """

    def __init__(
        self,
        log: EventLog,
        bus: CacheRefreshBus,
        session_manager: Optional[SessionManager] = None,
        profile_manager: Optional[ProfileManager] = None,
    ) -> None:
        # `log`, `session_manager`, `profile_manager` are
        # kept on the constructor for backward compatibility
        # with callers that still pass managers. They are
        # not used by `refresh_all` itself (refreshes go
        # through the bus); the Projector still uses them
        # for its own one-shot fold.
        self._log = log
        self._bus = bus
        self._sessions = session_manager
        self._profiles = profile_manager

    def refresh_all(self, world: World) -> list[Event]:
        """
        Walk the current world and, for every memory agent,
        enqueue a `CacheRefreshRequest` on the bus. Returns
        no events (this is a housekeeping pass, not domain
        logic).

        Pure: no I/O, no side effects beyond the bus
        mutation. The bus itself is in-memory and intended
        to be consumed in the same process.
        """
        for agent_id in world.agents:
            mem = parse_agent_id(agent_id)
            if mem is None:
                continue
            self._bus.publish(
                CacheRefreshRequest(
                    kind=mem.kind,
                    id1=mem.id1,
                    id2=mem.id2,
                )
            )
        return []

    def as_cyclic_system(self):
        """
        Returns a `CyclicSystem` callable that delegates to
        `refresh_all`. The returned callable is a plain
        `CyclicSystem` (pure): no I/O, no side effects
        beyond the in-memory bus.
        """
        cons = self

        async def _system(world: World) -> list[Event]:
            return cons.refresh_all(world)

        return _system


# -----------------------------------------------------------------------------
# Projector — one-shot fold → cache write
# -----------------------------------------------------------------------------


class Projector:
    """
    Forces a cache write for a given (session_id or
    tenant_id+user_id) by folding the EventLog and pushing
    the result to Redis.

    Useful in tests and for warmup scripts.

    The Projector is intentionally side-effecting (it writes
    to Redis directly). Unlike the Consolidator, it is a
    one-shot operation invoked from a test or a maintenance
    script, not a cyclic system that runs every tick.
    """

    def __init__(
        self,
        log: EventLog,
        session_manager: SessionManager,
        profile_manager: ProfileManager,
        continuity_manager: Optional["ContinuityManager"] = None,
    ) -> None:
        self._log = log
        self._sessions = session_manager
        self._profiles = profile_manager
        # Continuity manager é opcional por compatibilidade;
        # quando presente, ``project_all`` inclui o tier
        # ``continuity``. Veja ADR-014.
        self._continuity = continuity_manager

    async def project_session(self, session_id: str) -> bool:
        events = await self._log.read(SessionManager.agent_id_for(session_id))
        state = _fold_session_events(session_id, events)
        if state is None:
            return False
        await self._sessions.write_cache(session_id, state)
        return True

    async def project_profile(self, tenant_id: str, user_id: str) -> bool:
        events = await self._log.read(ProfileManager.agent_id_for(tenant_id, user_id))
        state = _fold_profile_events(tenant_id, user_id, events)
        if state is None:
            return False
        await self._profiles.write_cache(tenant_id, user_id, state)
        return True

    async def project_continuity(self, tenant_id: str, user_id: str) -> bool:
        """
        Project continuity state to Redis (ADR-014). Returns
        False if the manager is unconfigured or if no
        ``continuity.created`` event exists yet.
        """
        if self._continuity is None:
            return False
        from .continuity import (
            ContinuityManager,
            _fold_continuity_events,
        )

        events = await self._log.read(
            ContinuityManager.agent_id_for(tenant_id, user_id)
        )
        state = _fold_continuity_events(tenant_id, user_id, events)
        if state is None:
            return False
        await self._continuity.write_cache(tenant_id, user_id, state)
        return True

    async def project_all(self) -> dict[str, int]:
        """
        Project everything currently in the EventLog to the
        cache. Returns counts of (sessions, profiles,
        continuities) written.
        """
        counts = {"sessions": 0, "profiles": 0, "continuity": 0}
        for aid in await self._log.list_agents():
            mem = parse_agent_id(aid)
            if mem is None:
                continue
            if mem.kind == "session":
                if await self.project_session(mem.id1):
                    counts["sessions"] += 1
            elif mem.kind == "profile":
                if await self.project_profile(mem.id1, mem.id2):
                    counts["profiles"] += 1
            elif mem.kind == "continuity":
                if await self.project_continuity(mem.id1, mem.id2):
                    counts["continuity"] += 1
        return counts
