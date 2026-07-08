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
    return AgentView(
        agent_id=event.agent_id,
        components=_extract_components_from_event(event),
        operational_phase=prev.operational_phase,
        operational_at=prev.operational_at,
        domain_phase=event.event_type,
        domain_at=event.timestamp,
        last_event_id=str(event.event_id),
        last_event_at=event.timestamp,
    )


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
