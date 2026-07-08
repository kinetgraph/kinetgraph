# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Memory subsystem (F8.1 + ADR-014).

Provides the framework-level memory tiers (Redis-backed).
Each is modeled as an **agent** in the EventLog and
projected to a Redis cache:

  - SessionManager   (tier: session)   — short-term conversational
                                          memory. Redis JSON with
                                          TTL. See ADR-004 §2.1.
  - ProfileManager   (tier: profile)   — long-term **static**
                                          preferences of the PME
                                          (regime tributário, tier
                                          SLA, e-mail de NF-e).
                                          Redis Hash, sem TTL.
                                          See ADR-004 §2.1.
  - ContinuityManager (tier: continuity) — recent **state-of-use**
                                          (última tool, último
                                          cliente, último CFOP).
                                          Redis Hash with TTL
                                          sliding, PII hash-only,
                                          LGPD `cleared`. See
                                          ADR-014.

Plus the orchestration plumbing shared across the three
Redis-backed tiers (session, profile, continuity):

  - Consolidator   — pure cyclic system that publishes
                     CacheRefreshRequests on a bus.
  - CacheWarmer    — I/O adapter that consumes the bus and
                     applies refreshes to the Redis cache.
  - Projector      — one-shot fold → cache write.

Each manager treats its target as an **agent** in the
EventLog. The EventLog is the source of truth; Redis (JSON
for session, Hash for profile and continuity) is a TTL
cache that the manager maintains. The cache is always
reconstructable from the EventLog.

**Critério de seleção de tier** (ADR-014 §2.2):

- Estado muda **sem interação do usuário** (ex: tier SLA
  alterado por billing) → `profile`.
- Estado muda **em resposta a uma tool call** (ex: CFOP
  escolhido pelo agent na última NF-e) → `continuity`.
- Estado **carrega PII de terceiro** (ex: último CNPJ de
  cliente) → `continuity` com hash, não `profile`.
- Estado **efêmero** da conversa atual → `session`.
- Estado **agregado cross-agent** de tool calls →
  :mod:`kntgraph.agents.memory` (vertical — FalkorDB tier).
"""

from .cache_warmer import (
    CacheRefreshBus,
    CacheRefreshKind,
    CacheRefreshRequest,
    CacheWarmer,
)
from .consolidation import (
    Consolidator,
    Projector,
)
from .continuity import (
    CONTINUITY_KEY_PREFIX,
    ContinuityEventType,
    ContinuityManager,
    ContinuityState,
)
from .profile import (
    PROFILE_KEY_PREFIX,
    ProfileEventType,
    ProfileManager,
    ProfileState,
)
from .session import (
    SESSION_KEY_PREFIX,
    SessionEventType,
    SessionManager,
    SessionState,
)

__all__ = [
    # session
    "SESSION_KEY_PREFIX",
    "SessionEventType",
    "SessionManager",
    "SessionState",
    # profile
    "PROFILE_KEY_PREFIX",
    "ProfileEventType",
    "ProfileManager",
    "ProfileState",
    # continuity (ADR-014)
    "CONTINUITY_KEY_PREFIX",
    "ContinuityEventType",
    "ContinuityManager",
    "ContinuityState",
    # consolidation
    "Consolidator",
    "Projector",
    # cache warmer (bus + adapter)
    "CacheRefreshBus",
    "CacheRefreshKind",
    "CacheRefreshRequest",
    "CacheWarmer",
]
