# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
core.world._fold_state -- Typed fold-state shapes for the
three memory tiers (Audit Prioridade 4).

The fold state was previously a bare ``dict[str, Any]``
across nine sites in
``src/kntgraph/core/world/projection_memory.py`` (one
``_init_*_state`` + one ``_build_*_component`` per tier,
plus the per-event handler signatures and the handler
table). The audit listed the ``dict[str, Any]`` shape as
Prioridade 4: 9 sites of ``Mapping[str, Any]`` that
degraded the type signal at the fold's hottest
hot-path. Replacing the bare dicts with frozen
dataclasses restores the field-level types the audit
recommendation promised.

Three dataclasses, one per memory tier:

  - ``SessionFoldState`` -- the mutable accumulator
    inside the session fold. Field-level types match
    ``SessionComponent`` (the read-side projection).
  - ``ProfileFoldState`` -- the mutable accumulator
    inside the profile fold. Field-level types match
    ``ProfileComponent``.
  - ``ContinuityFoldState`` -- the mutable accumulator
    inside the continuity fold. Field-level types match
    ``ContinuityComponent``.

The dataclasses are ``frozen=True`` to keep the
"immutable in flight" guarantee at the object level
(matching the SessionComponent contract). The per-event
handlers do **not** mutate fields in place -- the fold
body builds a new state dataclass per event and replaces
the old one in the dict. The pattern:

    state = _init_session_state(base)
    for event in events:
        state = _SESSION_HANDLERS[event.event_type](event, state)
    return _build_session_component(agent_id, state)

is structurally identical to the previous dict-based
code (the ``_init_*_state`` + per-event handler
+ ``_build_*_component`` triad), but the field-level
types now flow through the type checker. Handlers
that read ``state.messages`` see ``tuple[dict[str,
JsonValue], ...]`` instead of ``Any``; handlers that
read ``state.context`` see ``dict[str, str]``; etc.

The audit's recommendation was ``TypedDict`` OR
``frozen dataclass``; the dataclass wins because:

  1. ``TypedDict`` requires ``typing.TypedDict`` (no
     runtime validation) -- a typo in a handler
     (``state["messsages"]``) is silent.
  2. A frozen dataclass is also a runtime check: an
     unexpected key raises ``AttributeError``.
  3. The fold runs in a hot path; ``__slots__=True`` on
     each dataclass keeps the per-step allocation
     cost bounded.
  4. The downstream ``_build_*_component`` builders
     accept the fold state by parameter type, so the
     type-checker enforces the round-trip
     ``init → handlers → build`` shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .._typing import JsonValue

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionFoldState:
    """The mutable accumulator inside the session fold.

    Field-level types mirror ``SessionComponent`` (the
    read-side projection). The fold's per-event handlers
    build a *new* ``SessionFoldState`` per event and
    replace the previous one; the dataclass is
    ``frozen`` so the type checker can reason about
    immutability across the loop.

    The ``messages`` field is the *mutable in-flight*
    list; the consumer (``_build_session_component``)
    converts it to a ``tuple`` (the read-side component
    stores messages as ``tuple[dict[str, JsonValue], ...]``).
    The intermediate ``list`` keeps the per-event append
    O(1); the build-time ``tuple(...)`` freezes the
    snapshot.
    """

    session_id: str = ""
    messages: list[dict[str, JsonValue]] = field(default_factory=list)
    context: dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float | None = None
    user_id: str = ""
    tenant_id: str = ""
    intent_event_id: str | None = None


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileFoldState:
    """The mutable accumulator inside the profile fold.

    Field-level types mirror ``ProfileComponent``. The
    flat KV (preferences + tier) means a single
    ``dict[str, str]`` carries the per-tier data; tier
    is a separate field (the lifecycle is independent
    of preferences, per ADR-042 §2.4).
    """

    preferences: dict[str, str] = field(default_factory=dict)
    tier: str = "standard"
    created_at: float = 0.0
    updated_at: float = 0.0
    user_id: str = ""
    tenant_id: str = ""


# ---------------------------------------------------------------------------
# Continuity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContinuityFoldState:
    """The mutable accumulator inside the continuity fold.

    Field-level types mirror ``ContinuityComponent``.
    The three ``last_*`` maps are the sliding-window
    lookups the continuity tier exposes; ``cleared_at``
    is the LGPD right-to-erasure sentinel (per the
    existing ``is_cleared`` method on the component).
    """

    last_tools: dict[str, str] = field(default_factory=dict)
    last_entities: dict[str, str] = field(default_factory=dict)
    last_categories: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    cleared_at: float | None = None
    user_id: str = ""
    tenant_id: str = ""


__all__ = [
    "ContinuityFoldState",
    "ProfileFoldState",
    "SessionFoldState",
]
