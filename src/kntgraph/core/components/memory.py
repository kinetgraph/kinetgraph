# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.components.memory -- ECS memory components (ADR-042 §2.1).

The three transient-memory tiers (Session, Profile,
Continuity — ADR-004, ADR-014) are projected onto
the ECS view as frozen dataclasses. They carry the
*current state* of each tier; the EventLog remains
the source of truth for history.

A system that wants to read a memory reads the
``SessionComponent`` / ``ProfileComponent`` /
``ContinuityComponent`` from the ``AgentView`` —
no Redis I/O inside the ``__call__`` (ADR-036).
The hydration step (T1.5 of ADR-042 §7) installs
these components on the view before the system
runs; the hydration projection is the only place
that touches Redis for memory.

**Field drift is forbidden.** The component fields
mirror the existing ``SessionState`` / ``ProfileState``
/ ``ContinuityState`` dataclasses in
``src/kntgraph/memory/``. Any change to a ``*State``
field requires a corresponding change here. The
acceptance checklist of ADR-042 §9 enforces this.

**Why these are Components, not raw dicts.** The
ECS pattern treats a memory read as a state
transition: the agent archetype gains a
``SessionComponent`` (or loses it on session end).
The system reads components by class — the
``isinstance(view.components[SessionComponent],
SessionComponent)`` pattern. A raw dict would
require string-compare (anti-pattern) and would
not benefit from archetype indexing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True, slots=True)
class SessionComponent:
    """
    Tier: Per-conversation state (ADR-004 §2.1).

    Projection of the *current* session — the last
    written value for each field. The full message
    history is in the EventLog; the component is
    the point-in-time snapshot a system reads.

    ``intent_event_id`` is the event_id of the
    ``user.intent`` event that triggered the
    in-flight system. It is the *causation_id*
    of the tool request the system emits and the
    stable handle for multi-turn chat (it does not
    change across ticks the way ``view.last_event_id``
    does).
    """

    session_id: str
    user_id: str
    tenant_id: str
    messages: tuple[dict, ...] = ()
    context: dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: Optional[float] = None
    intent_event_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ProfileComponent:
    """
    Tier: What the SME is (ADR-004 §2.2).

    Projection of the *current* profile — the last
    written value for each field. The component is
    a flat KV of preferences plus a billing-driven
    tier. The ``tier`` lifecycle is independent of
    ``preferences``; see ADR-042 §2.4 for the
    driver-by-driver contract.
    """

    tenant_id: str
    user_id: str
    preferences: dict[str, str] = field(default_factory=dict)
    tier: str = "standard"  # "vip" | "standard" | "basic"
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class ContinuityComponent:
    """
    Tier: What the SME was doing (ADR-014).

    Projection of the *current* continuity state —
    the sliding-window aggregation over recent
    ``continuity.*`` events. PII is hash-only; the
    LGPD ``cleared`` flag erases the state (see
    ADR-014 §2.7 for the PII gate).
    """

    tenant_id: str
    user_id: str
    last_tools: dict[str, str] = field(default_factory=dict)
    last_entities: dict[str, str] = field(default_factory=dict)
    last_categories: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    cleared_at: Optional[float] = None


__all__ = [
    "ContinuityComponent",
    "ProfileComponent",
    "SessionComponent",
]
