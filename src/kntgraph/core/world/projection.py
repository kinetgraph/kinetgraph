# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
world.projection -- The default fold of events into AgentViews.

The framework's projection rule is the "last event wins" fold:

  - **lifecycle** events update `operational_phase`
    and the operational timestamp; components and
    `domain_phase` carry over from the previous view.
  - **domain** events replace components with the
    event's data and update `domain_phase` /
    timestamp; `operational_phase` carries over.

`Projection` is the type alias for any callable
following the same contract — applications can
supply a richer projection via
`World.fold(events, projection=...)`.

`_lifecycle_phase_from_event` is the single source
of truth for the "agent.<verb>" → OperationalPhase
mapping. It reuses `OPERATIONAL_EVENT_TO_PHASE` from
``event.operational`` so a new framework event_type
is added in exactly one place.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..event import Event
from ..event.operational import OPERATIONAL_EVENT_TO_PHASE
from ..lifecycle import OperationalPhase
from .view import AgentView


# A projection is any callable from sequence[Event] to dict[agent_id, AgentView].
Projection = Callable[[Sequence[Event]], dict[str, AgentView]]


# A ``components`` key is PRESERVED across a domain
# ``_apply_event`` call when it appears in
# ``_DERIVED_COMPONENT_KEYS``. The default domain
# projection REPLACES ``components`` with the new
# event's data; derived components (installed by an
# orthogonal projection pass — e.g. the tool-call
# overlay or the memory hydration) are kept on the
# view so the system can read them in the same tick.
#
# The registry is intentionally explicit (not "all
# non-equal keys") because a domain event's data
# IS a component of the same name as the event
# type; if we just preserved every key, the new
# event's data would merge into the previous
# component instead of replacing it (which is the
# wrong semantics for the default fold).
#
# Add a key here when a projection installs a
# component that must survive the
# "last-event-wins" domain fold.
_DERIVED_COMPONENT_KEYS: frozenset[Any] = frozenset(
    {
        "tool_requests",
        "tool_completions",
        # Memory components (ADR-042). The default
        # projection does not know about
        # SessionComponent / ProfileComponent /
        # ContinuityComponent; the hydration
        # projection installs them and they must
        # survive the next domain fold (otherwise
        # a ``user.intent`` arriving on a fresh
        # tick would clobber the session).
        # The class identity is the key (the
        # components dict uses class objects as
        # keys, not strings, for typed components).
        # The import is deferred to avoid a
        # circular dependency; the actual lookup
        # happens in :func:`_is_derived_component_key`.
    }
)


def _is_derived_component_key(key: Any) -> bool:
    """True if ``key`` is a derived component that
    must survive the default domain fold.

    String keys are matched against
    :data:`_DERIVED_COMPONENT_KEYS`. Class keys
    (used by typed component projections like
    ADR-042) are matched by class identity
    against a small set of well-known component
    classes imported lazily.
    """
    if isinstance(key, str):
        return key in _DERIVED_COMPONENT_KEYS
    # Typed component (class key, ADR-042).
    if isinstance(key, type):
        try:
            from ..components import memory as _memory_components
        except ImportError:
            return False
        return key in {
            _memory_components.SessionComponent,
            _memory_components.ProfileComponent,
            _memory_components.ContinuityComponent,
        }
    return False


def _apply_event(prev: AgentView, event: Event) -> AgentView:
    """
    Build the new ``AgentView`` for ``event`` by folding
    it over ``prev``.

    The branch on ``event.event_class`` encodes the
    framework's projection rule:

      - **lifecycle**: update ``operational_phase`` and
        timestamp; keep components and domain_phase from
        ``prev``.
      - **domain**: replace components with the event's
        data; update ``domain_phase`` and timestamp;
        keep ``operational_phase`` from ``prev``.

    Either way, ``last_event_id`` and ``last_event_at``
    advance to the new event.

    **Derived components** (tool-call slots, memory
    components) are PRESERVED from ``prev`` across a
    domain fold: a domain event's data is installed
    as a new component (keyed by ``event.event_type``),
    but the previous tick's derived components
    (installed by an orthogonal projection pass —
    e.g. ``overlay_tool_calls`` or ``project_memory``)
    are kept on the view. This is the
    "accumulation" contract: the default fold does
    not know about derived components, but it must
    not destroy them. See ADR-044 (tool-call
    overlay) and ADR-042 §6.1 (memory hydration).

    This is the single source of truth for the
    "lifecycle vs. domain" projection. Both
    :func:`project_default` (batch fold) and
    :meth:`World.with_event` (single-event) call it so
    a change to the rule only has to happen here.
    """
    if event.event_class == "lifecycle":
        phase = _lifecycle_phase_from_event(event.event_type)
        return AgentView(
            agent_id=event.agent_id,
            components=prev.components,
            operational_phase=phase,
            operational_at=event.timestamp,
            domain_phase=prev.domain_phase,
            domain_at=prev.domain_at,
            last_event_id=str(event.event_id),
            last_event_at=event.timestamp,
        )
    # "domain"
    new_components = _extract_components_from_event(event)
    # Preserve derived components (tool slots,
    # memory components) from the previous view.
    # See :data:`_DERIVED_COMPONENT_KEYS` and
    # :func:`_is_derived_component_key`.
    preserved: dict[Any, Any] = {
        k: v for k, v in prev.components.items() if _is_derived_component_key(k)
    }
    if preserved:
        new_components = dict(new_components)
        new_components.update(preserved)
    return AgentView(
        agent_id=event.agent_id,
        components=new_components,
        operational_phase=prev.operational_phase,
        operational_at=prev.operational_at,
        domain_phase=event.event_type,
        domain_at=event.timestamp,
        last_event_id=str(event.event_id),
        last_event_at=event.timestamp,
    )


def _is_tool_event(event_type: str) -> bool:
    """True if the event type is a tool event.

    Matches the canonical ``tool.<name>.<suffix>`` form
    (ADR-036) and the legacy bare form. Currently
    informational; the preservation rule is now
    component-key-based (see :data:`_DERIVED_COMPONENT_KEYS`)
    and no longer branches on the event type.
    """
    if event_type == "tool.requested":
        return True
    if event_type == "tool.completed":
        return True
    if event_type == "tool.failed":
        return True
    if event_type.startswith("tool."):
        suffix = event_type.rsplit(".", 1)[-1]
        if suffix in ("requested", "completed", "failed"):
            return True
    return False


def project_default(events: Sequence[Event]) -> dict[str, AgentView]:
    """
    Default projection: a single fold over events.

    - For each event, the agent's `last_event_*` and `last_event_at`
      are updated.
    - For lifecycle events, `operational_phase` is set to
      `event_type` (e.g. "agent.spawned" → "spawned") and timestamp
      recorded.
    - For domain events, `domain_phase` is set to `event_type` (e.g.
      "document.validated" → "validated") and `components` is replaced
      with `event.data` (the latest snapshot wins).

    This is intentionally simple. Applications can supply a richer
    projection (see `World.from_events(..., projection=...)`).
    """
    views: dict[str, AgentView] = {}
    for e in events:
        prev = views.get(e.agent_id) or AgentView(agent_id=e.agent_id)
        views[e.agent_id] = _apply_event(prev, e)
    return views


def _lifecycle_phase_from_event(event_type: str) -> OperationalPhase:
    """
    Map an event_type from the "lifecycle" namespace to an
    OperationalPhase. The convention is:

        agent.spawned     → "spawned"
        agent.idle        → "idle"
        agent.running     → "running"
        agent.blocked     → "blocked"
        agent.checkpointed→ "checkpointed"
        agent.terminated  → "terminated"

    For unrecognized types, the phase is the raw suffix after the dot.

    Reuses `OPERATIONAL_EVENT_TO_PHASE` from ``event.operational``
    so the closed set of operational event types is defined in
    exactly one place.
    """
    if event_type in OPERATIONAL_EVENT_TO_PHASE:
        # `OPERATIONAL_EVENT_TO_PHASE` is typed `dict[str, str]`
        # but the framework's `OperationalPhase` is a `Literal`
        # of those same string values. Returning the str
        # directly is the contract; the cast is implicit
        # through the function's `-> OperationalPhase` return
        # annotation.
        return OPERATIONAL_EVENT_TO_PHASE[event_type]  # type: ignore[return-value]
    # Fallback: last token
    return event_type.rsplit(".", 1)[-1]  # type: ignore[return-value]


def _extract_components_from_event(event: Event) -> dict[str, Any]:
    """
    Map the event payload into a components dict.

    The default strategy is: each event becomes a single component
    named after the event_type, whose value is the event's data
    payload. Applications that want richer projections override this.
    """
    return {event.event_type: dict(event.data)}


__all__ = [
    "Projection",
    "project_default",
]
