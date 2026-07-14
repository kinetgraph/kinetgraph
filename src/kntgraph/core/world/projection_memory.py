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

from collections.abc import Mapping, Sequence
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
    """
    messages: list[dict] = []
    context: dict[str, str] = {}
    started_at: float = 0.0
    ended_at: float | None = None
    user_id = ""
    tenant_id = ""
    intent_event_id: str | None = None

    if base_session is not None:
        # Reuse the base state. The fold will
        # overwrite the fields below if the
        # batch re-derives them; everything else
        # is preserved.
        messages = list(base_session.messages)
        context = dict(base_session.context)
        started_at = base_session.started_at
        ended_at = base_session.ended_at
        user_id = base_session.user_id
        tenant_id = base_session.tenant_id
        # The ``intent_event_id`` is the last
        # domain event in the batch; we update
        # it below if the batch has any domain
        # event. We start with the base value
        # so a batch with no domain events keeps
        # the previous intent_event_id (the
        # system is still reacting to the same
        # intent across ticks).
        intent_event_id = base_session.intent_event_id

    for e in events:
        if e.event_type == SESSION_STARTED:
            started_at = e.timestamp
            user_id = str(e.data.get("user_id", user_id))
            tenant_id = str(e.data.get("tenant_id", tenant_id))
        elif e.event_type == SESSION_MESSAGE:
            messages.append(
                {
                    "role": e.data.get("role", "user"),
                    "content": e.data.get("content", ""),
                }
            )
        elif e.event_type == SESSION_CONTEXT:
            key = str(e.data.get("key", ""))
            if key:
                context[key] = str(e.data.get("value", ""))
        elif e.event_type == SESSION_ENDED:
            ended_at = e.timestamp

    if started_at == 0.0:
        return None

    # The ``intent_event_id`` is the event_id of
    # the last ``user.intent`` (or similar
    # INTENT-class) event in the batch. We
    # EXCLUDE ``session.*`` / ``profile.*`` /
    # ``continuity.*`` events (they are bookkeeping
    # for the session itself) AND ``tool.*`` events
    # (they are worker-pool round-trips, not
    # user-driven intent signals; the LLM's
    # ``tool.chat_llm.requested`` event lands in the
    # same batch as the user.intent and would
    # otherwise clobber the intent_event_id).
    for e in events:
        if e.event_class != "domain":
            continue
        if e.event_type.startswith(("session.", "profile.", "continuity.", "tool.")):
            continue
        intent_event_id = str(e.event_id)

    # If the batch had no domain event, keep
    # the base ``intent_event_id`` (we are
    # still reacting to the same intent).
    # Otherwise the last domain event wins.

    return SessionComponent(
        session_id=agent_id.removeprefix("session:")
        if agent_id.startswith("session:")
        else agent_id,
        user_id=user_id,
        tenant_id=tenant_id,
        messages=tuple(messages),
        context=context,
        started_at=started_at,
        ended_at=ended_at,
        intent_event_id=intent_event_id,
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
    """
    preferences: dict[str, str] = {}
    tier = "standard"
    created_at: float = 0.0
    updated_at: float = 0.0
    tenant_id = ""
    user_id = ""

    if base_profile is not None:
        # Reuse the base state. The fold will
        # overwrite the fields below if the batch
        # re-derives them.
        preferences = dict(base_profile.preferences)
        tier = base_profile.tier
        created_at = base_profile.created_at
        updated_at = base_profile.updated_at
        tenant_id = base_profile.tenant_id
        user_id = base_profile.user_id

    for e in events:
        if e.event_type == PROFILE_CREATED:
            created_at = e.timestamp
            tenant_id = str(e.data.get("tenant_id", tenant_id))
            user_id = str(e.data.get("user_id", user_id))
            initial = e.data.get("preferences") or {}
            if isinstance(initial, dict):
                for k, v in initial.items():
                    preferences[str(k)] = str(v)
            tier = str(e.data.get("tier", tier))
        elif e.event_type == PROFILE_PREFERENCE_SET:
            k = str(e.data.get("key", ""))
            v = str(e.data.get("value", ""))
            if k:
                preferences[k] = v
            updated_at = e.timestamp
        elif e.event_type == PROFILE_PREFERENCE_UNSET:
            k = str(e.data.get("key", ""))
            if k:
                preferences.pop(k, None)
            updated_at = e.timestamp
        elif e.event_type == PROFILE_TIER_CHANGED:
            tier = str(e.data.get("tier", tier))
            updated_at = e.timestamp

    if created_at == 0.0:
        return None

    return ProfileComponent(
        tenant_id=tenant_id,
        user_id=user_id,
        preferences=preferences,
        tier=tier,
        created_at=created_at,
        updated_at=updated_at,
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
    """
    last_tools: dict[str, str] = {}
    last_entities: dict[str, str] = {}
    last_categories: dict[str, str] = {}
    created_at: float = 0.0
    updated_at: float = 0.0
    cleared_at: float | None = None
    tenant_id = ""
    user_id = ""

    if base_continuity is not None:
        last_tools = dict(base_continuity.last_tools)
        last_entities = dict(base_continuity.last_entities)
        last_categories = dict(base_continuity.last_categories)
        created_at = base_continuity.created_at
        updated_at = base_continuity.updated_at
        cleared_at = base_continuity.cleared_at
        tenant_id = base_continuity.tenant_id
        user_id = base_continuity.user_id

    for e in events:
        if e.event_type == CONTINUITY_CREATED:
            created_at = e.timestamp
            tenant_id = str(e.data.get("tenant_id", tenant_id))
            user_id = str(e.data.get("user_id", user_id))
        elif e.event_type == CONTINUITY_TOOL_USED:
            tool = str(e.data.get("tool", ""))
            if tool:
                last_tools[tool] = f"{e.data.get('result_signature', '')}|{e.timestamp}"
            updated_at = e.timestamp
        elif e.event_type == CONTINUITY_ENTITY_SEEN:
            kind = str(e.data.get("kind", ""))
            value_hash = str(e.data.get("value_hash", ""))
            if kind and value_hash:
                last_entities[f"{kind}:{value_hash[:16]}"] = value_hash
            updated_at = e.timestamp
        elif e.event_type == CONTINUITY_CATEGORY_CHOSEN:
            slot = str(e.data.get("slot", ""))
            value = str(e.data.get("value", ""))
            if slot:
                last_categories[slot] = f"{value}|{e.timestamp}"
            updated_at = e.timestamp
        elif e.event_type == CONTINUITY_CLEARED:
            cleared_at = e.timestamp
            last_tools.clear()
            last_entities.clear()
            last_categories.clear()

    if created_at == 0.0:
        return None

    return ContinuityComponent(
        tenant_id=tenant_id,
        user_id=user_id,
        last_tools=last_tools,
        last_entities=last_entities,
        last_categories=last_categories,
        created_at=created_at,
        updated_at=updated_at,
        cleared_at=cleared_at,
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
    # Group events by agent_id once. We can then
    # build a per-agent memory fold without
    # iterating the full batch per agent.
    events_by_agent: dict[str, list[Event]] = {}
    for e in events:
        events_by_agent.setdefault(e.agent_id, []).append(e)

    out: dict[str, AgentView] = dict(base_views)
    for agent_id, agent_events in events_by_agent.items():
        # The base_view (if any) carries the
        # *previous* SessionComponent /
        # ProfileComponent / ContinuityComponent
        # from the last fold. The current batch
        # may or may not include the events that
        # re-derive those components; if it does
        # not, we KEEP the base component (no
        # re-derivation needed). If it does (e.g.
        # a new ``session.message`` lands), we
        # re-fold using the new batch.
        base_view = base_views.get(agent_id, AgentView(agent_id=agent_id))
        existing_session = base_view.components.get(SessionComponent)
        existing_profile = base_view.components.get(ProfileComponent)
        existing_continuity = base_view.components.get(ContinuityComponent)

        session = _fold_session(agent_id, agent_events, base_session=existing_session)
        profile = _fold_profile(agent_id, agent_events, base_profile=existing_profile)
        continuity = _fold_continuity(
            agent_id, agent_events, base_continuity=existing_continuity
        )

        # If the current batch did not re-derive
        # the component (i.e. the fold returned
        # None because no memory event landed),
        # keep the base component.
        if session is None:
            session = existing_session
        if profile is None:
            profile = existing_profile
        if continuity is None:
            continuity = existing_continuity

        if session is None and profile is None and continuity is None:
            continue

        new_components: dict[str, Any] = dict(base_view.components)
        if session is not None:
            new_components[SessionComponent] = session
        if profile is not None:
            new_components[ProfileComponent] = profile
        if continuity is not None:
            new_components[ContinuityComponent] = continuity
        out[agent_id] = replace(base_view, components=new_components)
    return out


__all__ = ["project_memory"]
