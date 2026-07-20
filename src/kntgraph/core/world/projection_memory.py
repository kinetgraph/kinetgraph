# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.world.projection_memory -- memory hydration projection
(ADR-042 §2.1, §6.1).

A pure projection that walks the agent's ``session.*``,
``profile.*``, and ``continuity.*`` events and
materialises ``SessionComponent``, ``ProfileComponent``,
and ``ContinuityComponent`` on the ``AgentView``.

This is the **in-memory hydration step** (T1.5 of
ADR-042 §7). It is **pure**: the projection walks
events, never touches Redis. The cache lives
elsewhere (the ``SessionManager.read`` read-through
cache) and is a write-through detail visible to
the recorder tool, not to the system.

Composition with other projections:

  - The default projection (last-event-wins per
    event type) is applied first to produce the
    base view.
  - The memory projection OVERLAYS the
    ``SessionComponent`` / ``ProfileComponent`` /
    ``ContinuityComponent`` slots on the view.
    Other slots are untouched.
  - The tool-call overlay (ADR-034) is applied
    LAST, after the memory hydration. This order
    is important: the memory projection may
    consume the same events the tool-call overlay
    needs (``continuity.entity_seen`` etc. are
    BOTH memory and tool events, depending on the
    event namespace). In practice the namespaces
    are disjoint: ``session.*`` / ``profile.*`` /
    ``continuity.*`` are memory-only, and
    ``tool.<name>.*`` are tool-only.

**Multi-turn safe.** Because the projection walks
the full event batch (not just the latest), it
reconstructs the *current* state (last write wins
per field) deterministically. Re-folding the same
batch produces the same components.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..components.memory import (
    ContinuityComponent,
    ProfileComponent,
    SessionComponent,
)
from ..event import Event
from .view import AgentView

SESSION_STARTED = "session.started"
SESSION_MESSAGE = "session.message"
SESSION_CONTEXT = "session.context"
SESSION_ENDED = "session.ended"

PROFILE_CREATED = "profile.created"
PROFILE_PREFERENCE_SET = "profile.preference_set"
PROFILE_PREFERENCE_UNSET = "profile.preference_unset"
PROFILE_TIER_CHANGED = "profile.tier_changed"

CONTINUITY_CREATED = "continuity.created"
CONTINUITY_TOOL_USED = "continuity.tool_used"
CONTINUITY_ENTITY_SEEN = "continuity.entity_seen"
CONTINUITY_CATEGORY_CHOSEN = "continuity.category_chosen"
CONTINUITY_CLEARED = "continuity.cleared"


def _fold_session(
    agent_id: str,
    events: Sequence[Event],
    base_session: SessionComponent | None = None,
) -> SessionComponent | None:
    """Pure fold of ``session.*`` events → SessionComponent.

    If ``base_session`` is provided, the fold REUSES the
    state of the base component for fields that are
    NOT explicitly re-derived by the events in this
    batch. The one field that IS updated by *any*
    domain event in the batch is ``intent_event_id``:
    it always points to the last domain event in the
    batch (the user.intent that triggered this turn).

    Per-event dispatch is delegated to a small table of
    handlers (``_SESSION_HANDLERS``) so the fold itself
    stays a linear ``for`` loop and under the CC ≤ 10
    ceiling.
    """
    state = _init_session_state(base_session)

    for e in events:
        handler = _SESSION_HANDLERS.get(e.event_type)
        if handler is not None:
            handler(e, state)

    if state["started_at"] == 0.0:
        return None

    state["intent_event_id"] = _compute_intent_event_id(events, state)
    return _build_session_component(agent_id, state)


def _init_session_state(
    base_session: SessionComponent | None,
) -> dict[str, Any]:
    """Initialise the fold's mutable state from the
    base component (or from scratch when no base is
    given). The state is a dict (not a dataclass) so
    the per-event handlers can mutate fields without
    rebuilding the state object every step."""
    if base_session is None:
        return {
            "messages": [],
            "context": {},
            "started_at": 0.0,
            "ended_at": None,
            "user_id": "",
            "tenant_id": "",
            "intent_event_id": None,
        }
    return {
        "messages": list(base_session.messages),
        "context": dict(base_session.context),
        "started_at": base_session.started_at,
        "ended_at": base_session.ended_at,
        "user_id": base_session.user_id,
        "tenant_id": base_session.tenant_id,
        "intent_event_id": base_session.intent_event_id,
    }


def _on_session_started(e: Event, state: dict[str, Any]) -> None:
    """``session.started`` handler: stamp the start
    time and capture the identity fields. ``user_id``
    / ``tenant_id`` default to the current state so
    a re-derived identity does not clobber the base
    (e.g. when the batch only carries the timestamp)."""
    state["started_at"] = e.timestamp.timestamp()
    state["user_id"] = str(e.data.get("user_id", state["user_id"]))
    state["tenant_id"] = str(e.data.get("tenant_id", state["tenant_id"]))


def _on_session_message(e: Event, state: dict[str, Any]) -> None:
    """``session.message`` handler: append a single
    message. ``role`` defaults to ``"user"`` when the
    event payload omits it (the fold is permissive on
    the wire format; the canonical enforcer is upstream
    in the recorder)."""
    state["messages"].append(
        {
            "role": e.data.get("role", "user"),
            "content": e.data.get("content", ""),
        }
    )


def _on_session_context(e: Event, state: dict[str, Any]) -> None:
    """``session.context`` handler: write a key/value
    pair. Empty keys are dropped (the Redis schema
    disallows them; a defensive guard here keeps the
    fold idempotent)."""
    key = str(e.data.get("key", ""))
    if key:
        state["context"][key] = str(e.data.get("value", ""))


def _on_session_ended(e: Event, state: dict[str, Any]) -> None:
    """``session.ended`` handler: stamp the end time."""
    state["ended_at"] = e.timestamp.timestamp()


_SESSION_HANDLERS: dict[str, "Callable[[Event, dict[str, Any]], None]"] = {
    SESSION_STARTED: _on_session_started,
    SESSION_MESSAGE: _on_session_message,
    SESSION_CONTEXT: _on_session_context,
    SESSION_ENDED: _on_session_ended,
}


def _compute_intent_event_id(
    events: Sequence[Event],
    state: dict[str, Any],
) -> str | None:
    """The ``intent_event_id`` is the event_id of the
    last domain event in the batch, EXCLUDING
    ``session.*`` / ``profile.*`` / ``continuity.*``
    (bookkeeping) and ``tool.*`` (worker round-trips).
    If the batch had no domain event, keep the base
    value (we are still reacting to the same intent)."""
    last_intent: str | None = state["intent_event_id"]
    for e in events:
        if e.event_class != "domain":
            continue
        if e.event_type.startswith(("session.", "profile.", "continuity.", "tool.")):
            continue
        last_intent = str(e.event_id)
    return last_intent


def _build_session_component(agent_id: str, state: dict[str, Any]) -> SessionComponent:
    """Materialise the ``SessionComponent`` from the
    fold state. The ``session_id`` is the agent_id
    minus the ``session:`` prefix (the convention is
    that ``session:<id>`` is the agent namespace and
    the bare id is the session id)."""
    session_id = (
        agent_id.removeprefix("session:")
        if agent_id.startswith("session:")
        else agent_id
    )
    return SessionComponent(
        session_id=session_id,
        user_id=state["user_id"],
        tenant_id=state["tenant_id"],
        messages=tuple(state["messages"]),
        context=state["context"],
        started_at=state["started_at"],
        ended_at=state["ended_at"],
        intent_event_id=state["intent_event_id"],
    )


def _fold_profile(
    agent_id: str,
    events: Sequence[Event],
    base_profile: ProfileComponent | None = None,
) -> ProfileComponent | None:
    """Pure fold of ``profile.*`` events → ProfileComponent.

    If ``base_profile`` is provided, the fold REUSES
    the state of the base component for fields that
    are NOT explicitly re-derived by the events in
    this batch (preferences, tier, etc.).

    Per-event dispatch is delegated to a small table of
    handlers (``_PROFILE_HANDLERS``) so the fold itself
    stays a linear ``for`` loop and under the CC ≤ 10
    ceiling.
    """
    state = _init_profile_state(base_profile)

    for e in events:
        handler = _PROFILE_HANDLERS.get(e.event_type)
        if handler is not None:
            handler(e, state)

    if state["created_at"] == 0.0:
        return None

    return _build_profile_component(state)


def _init_profile_state(
    base_profile: ProfileComponent | None,
) -> dict[str, Any]:
    """Initialise the fold's mutable state from the
    base component (or from scratch when no base is
    given). The state dict lets the per-event
    handlers mutate fields without rebuilding the
    state object every step."""
    if base_profile is None:
        return {
            "preferences": {},
            "tier": "standard",
            "created_at": 0.0,
            "updated_at": 0.0,
            "tenant_id": "",
            "user_id": "",
        }
    return {
        "preferences": dict(base_profile.preferences),
        "tier": base_profile.tier,
        "created_at": base_profile.created_at,
        "updated_at": base_profile.updated_at,
        "tenant_id": base_profile.tenant_id,
        "user_id": base_profile.user_id,
    }


def _on_profile_created(e: Event, state: dict[str, Any]) -> None:
    """``profile.created`` handler: stamp the
    creation time, capture the identity, and seed
    preferences + tier from the event payload."""
    state["created_at"] = e.timestamp.timestamp()
    state["tenant_id"] = str(e.data.get("tenant_id", state["tenant_id"]))
    state["user_id"] = str(e.data.get("user_id", state["user_id"]))
    initial = e.data.get("preferences") or {}
    if isinstance(initial, dict):
        for k, v in initial.items():
            state["preferences"][str(k)] = str(v)
    state["tier"] = str(e.data.get("tier", state["tier"]))


def _on_profile_preference_set(e: Event, state: dict[str, Any]) -> None:
    """``profile.preference_set`` handler: write a
    single preference. Empty keys are dropped
    (defensive; the canonical enforcer is upstream
    in the recorder)."""
    k = str(e.data.get("key", ""))
    if k:
        state["preferences"][k] = str(e.data.get("value", ""))
    state["updated_at"] = e.timestamp.timestamp()


def _on_profile_preference_unset(e: Event, state: dict[str, Any]) -> None:
    """``profile.preference_unset`` handler: drop a
    single preference. Missing keys are silently
    ignored (Redis HDEL semantics)."""
    k = str(e.data.get("key", ""))
    if k:
        state["preferences"].pop(k, None)
    state["updated_at"] = e.timestamp.timestamp()


def _on_profile_tier_changed(e: Event, state: dict[str, Any]) -> None:
    """``profile.tier_changed`` handler: update the
    tier scalar. The default tier (``"standard"``)
    is preserved when the event omits a target."""
    state["tier"] = str(e.data.get("tier", state["tier"]))
    state["updated_at"] = e.timestamp.timestamp()


_PROFILE_HANDLERS: dict[str, "Callable[[Event, dict[str, Any]], None]"] = {
    PROFILE_CREATED: _on_profile_created,
    PROFILE_PREFERENCE_SET: _on_profile_preference_set,
    PROFILE_PREFERENCE_UNSET: _on_profile_preference_unset,
    PROFILE_TIER_CHANGED: _on_profile_tier_changed,
}


def _build_profile_component(state: dict[str, Any]) -> ProfileComponent:
    """Materialise the ``ProfileComponent`` from the
    fold state. The identity (``tenant_id`` /
    ``user_id``) and the timestamps come straight
    from the state dict; the per-event handlers are
    responsible for the values."""
    return ProfileComponent(
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        preferences=state["preferences"],
        tier=state["tier"],
        created_at=state["created_at"],
        updated_at=state["updated_at"],
    )


def _fold_continuity(
    agent_id: str,
    events: Sequence[Event],
    base_continuity: ContinuityComponent | None = None,
) -> ContinuityComponent | None:
    """Pure fold of ``continuity.*`` events → ContinuityComponent.

    If ``base_continuity`` is provided, the fold
    REUSES the state of the base component for
    fields that are NOT explicitly re-derived by
    the events in this batch (last_tools /
    last_entities / last_categories / cleared_at).

    Per-event dispatch is delegated to a small table of
    handlers (``_CONTINUITY_HANDLERS``) so the fold
    itself stays a linear ``for`` loop and under the
    CC ≤ 10 ceiling.
    """
    state = _init_continuity_state(base_continuity)

    for e in events:
        handler = _CONTINUITY_HANDLERS.get(e.event_type)
        if handler is not None:
            handler(e, state)

    if state["created_at"] == 0.0:
        return None

    return _build_continuity_component(state)


def _init_continuity_state(
    base_continuity: ContinuityComponent | None,
) -> dict[str, Any]:
    """Initialise the fold's mutable state from the
    base component (or from scratch when no base is
    given). The state dict lets the per-event
    handlers mutate fields without rebuilding the
    state object every step."""
    if base_continuity is None:
        return {
            "last_tools": {},
            "last_entities": {},
            "last_categories": {},
            "created_at": 0.0,
            "updated_at": 0.0,
            "cleared_at": None,
            "tenant_id": "",
            "user_id": "",
        }
    return {
        "last_tools": dict(base_continuity.last_tools),
        "last_entities": dict(base_continuity.last_entities),
        "last_categories": dict(base_continuity.last_categories),
        "created_at": base_continuity.created_at,
        "updated_at": base_continuity.updated_at,
        "cleared_at": base_continuity.cleared_at,
        "tenant_id": base_continuity.tenant_id,
        "user_id": base_continuity.user_id,
    }


def _on_continuity_created(e: Event, state: dict[str, Any]) -> None:
    """``continuity.created`` handler: stamp the
    creation time and capture the identity. The
    three ``last_*`` maps are seeded empty by the
    fold state and grow as the agent uses tools /
    sees entities / chooses categories."""
    state["created_at"] = e.timestamp.timestamp()
    state["tenant_id"] = str(e.data.get("tenant_id", state["tenant_id"]))
    state["user_id"] = str(e.data.get("user_id", state["user_id"]))


def _on_continuity_tool_used(e: Event, state: dict[str, Any]) -> None:
    """``continuity.tool_used`` handler: record the
    last usage of a tool. The value is a pipe-
    separated ``<result_signature>|<timestamp>`` so
    a second call with the same signature is
    idempotent (the timestamp rolls forward)."""
    tool = str(e.data.get("tool", ""))
    if tool:
        state["last_tools"][tool] = (
            f"{e.data.get('result_signature', '')}|{e.timestamp}"
        )
    state["updated_at"] = e.timestamp.timestamp()


def _on_continuity_entity_seen(e: Event, state: dict[str, Any]) -> None:
    """``continuity.entity_seen`` handler: record
    the PII-hash of an entity the agent saw. The
    key is ``<kind>:<value_hash[:16]>`` so the map
    stays bounded (the prefix is the natural bucket
    key; the full hash is the value)."""
    kind = str(e.data.get("kind", ""))
    value_hash = str(e.data.get("value_hash", ""))
    if kind and value_hash:
        state["last_entities"][f"{kind}:{value_hash[:16]}"] = value_hash
    state["updated_at"] = e.timestamp.timestamp()


def _on_continuity_category_chosen(e: Event, state: dict[str, Any]) -> None:
    """``continuity.category_chosen`` handler:
    record a slot's last-chosen category. Empty
    slots are dropped (the canonical enforcer is
    upstream in the semantic router)."""
    slot = str(e.data.get("slot", ""))
    if slot:
        state["last_categories"][slot] = f"{e.data.get('value', '')}|{e.timestamp}"
    state["updated_at"] = e.timestamp.timestamp()


def _on_continuity_cleared(e: Event, state: dict[str, Any]) -> None:
    """``continuity.cleared`` handler: LGPD
    forget-me-now — stamp the ``cleared_at`` and
    drop the three last-* maps. The fold keeps the
    creation identity (the LGPD request is a
    forgetting operation, not a deletion of the
    audit trail)."""
    state["cleared_at"] = e.timestamp.timestamp()
    state["last_tools"].clear()
    state["last_entities"].clear()
    state["last_categories"].clear()


_CONTINUITY_HANDLERS: dict[str, "Callable[[Event, dict[str, Any]], None]"] = {
    CONTINUITY_CREATED: _on_continuity_created,
    CONTINUITY_TOOL_USED: _on_continuity_tool_used,
    CONTINUITY_ENTITY_SEEN: _on_continuity_entity_seen,
    CONTINUITY_CATEGORY_CHOSEN: _on_continuity_category_chosen,
    CONTINUITY_CLEARED: _on_continuity_cleared,
}


def _build_continuity_component(state: dict[str, Any]) -> ContinuityComponent:
    """Materialise the ``ContinuityComponent`` from
    the fold state. The three ``last_*`` maps and
    the timestamps come straight from the state
    dict; the per-event handlers are responsible
    for the values."""
    return ContinuityComponent(
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        last_tools=state["last_tools"],
        last_entities=state["last_entities"],
        last_categories=state["last_categories"],
        created_at=state["created_at"],
        updated_at=state["updated_at"],
        cleared_at=state["cleared_at"],
    )


def project_memory(
    events: Sequence[Event],
    base_views: "Mapping[str, AgentView] | None" = None,
) -> dict[str, AgentView]:
    """
    Pure fold: events → AgentView with memory
    components installed on the relevant agents.

    The function is composed with the base
    projection (default or any custom one) by
    passing the base views as the second argument.
    The returned dict mirrors the input (every
    base view is in the output) and overlays the
    memory components on agents whose events
    included a memory namespace.

    Agents whose events did not include a memory
    namespace are passed through unchanged (no
    allocation).
    """
    base_views = base_views or {}
    events_by_agent = _group_events_by_agent(events)

    out: dict[str, AgentView] = dict(base_views)
    for agent_id, agent_events in events_by_agent.items():
        updated = _project_memory_for_agent(agent_id, agent_events, base_views)
        if updated is not None:
            out[agent_id] = updated
    return out


def _group_events_by_agent(
    events: Sequence[Event],
) -> dict[str, list[Event]]:
    """Group the batch by ``event.agent_id`` so the
    per-agent fold below does not iterate the full
    batch per agent. Allocation is one ``list`` per
    agent that has events in the batch; agents with
    no events do not appear in the dict (and are
    passed through unchanged in ``project_memory``)."""
    events_by_agent: dict[str, list[Event]] = {}
    for e in events:
        events_by_agent.setdefault(e.agent_id, []).append(e)
    return events_by_agent


def _project_memory_for_agent(
    agent_id: str,
    agent_events: list[Event],
    base_views: "Mapping[str, AgentView]",
) -> "AgentView | None":
    """Build the memory component overlay for a
    single agent. Returns ``None`` when the batch
    has no memory event AND the base view has no
    memory component (the caller then keeps the
    base view unchanged — no allocation)."""
    base_view = base_views.get(agent_id, AgentView(agent_id=agent_id))
    session, profile, continuity = _fold_memory_components(
        agent_id, agent_events, base_view
    )
    if session is None and profile is None and continuity is None:
        return None
    return _with_memory_components(base_view, session, profile, continuity)


def _fold_memory_components(
    agent_id: str,
    agent_events: list[Event],
    base_view: "AgentView",
) -> "tuple[SessionComponent | None, ProfileComponent | None, ContinuityComponent | None]":
    """Run the three memory folds for a single
    agent, threading the base components through so
    the batch can re-use state that the events do
    not re-derive. The fold functions return ``None``
    when the batch had no event of the matching
    type; the caller decides whether to keep the
    base component or drop it."""
    existing_session = base_view.components.get(SessionComponent)
    existing_profile = base_view.components.get(ProfileComponent)
    existing_continuity = base_view.components.get(ContinuityComponent)

    session = _fold_session(agent_id, agent_events, base_session=existing_session)
    profile = _fold_profile(agent_id, agent_events, base_profile=existing_profile)
    continuity = _fold_continuity(
        agent_id, agent_events, base_continuity=existing_continuity
    )
    return session, profile, continuity


def _with_memory_components(
    base_view: "AgentView",
    session: SessionComponent | None,
    profile: ProfileComponent | None,
    continuity: ContinuityComponent | None,
) -> "AgentView":
    """Return a new ``AgentView`` carrying the
    three memory components (the ones that the fold
    re-derived or the base ones the fold preserved).
    Components that the fold did not re-derive are
    passed through from the base view so the rest
    of the agent's state survives a memory-only
    batch (ADR-042 §6.1)."""
    new_components: dict[str | type[Any], Any] = dict(base_view.components)
    if session is not None:
        new_components[SessionComponent] = session
    if profile is not None:
        new_components[ProfileComponent] = profile
    if continuity is not None:
        new_components[ContinuityComponent] = continuity
    return replace(base_view, components=new_components)


__all__ = ["project_memory"]
