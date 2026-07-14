# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.world.projection_tool_calls -- tool-call projection.

Iter 28 FU 8 (ADR-034): this projection materialises
``ToolCallRequest`` and ``ToolCallCompletion`` components
from tool events. It is a PURE function: deterministic,
replayable, no side effects.

ADR-036 (Tool Worker Pattern) makes the
``tool.<name>.<suffix>`` form canonical. Both the request
matcher (:func:`_requested_tool_name`) and the completion
matcher (:func:`_completion_status`) accept the
WorkerManager form ``tool.<name>.completed`` /
``tool.<name>.failed`` / ``tool.<name>.requested`` in
addition to the legacy bare forms ``tool.requested`` /
``tool.completed`` / ``tool.failed``. The tool name is
captured from the event type's middle segment; the rest
of the payload is unchanged.

The legacy bare form is kept for **back-compat with old
EventLogs** (events written before the WorkerManager
migration). New emitters MUST use the ``tool.<name>.*``
form (see ``ToolAwareSystem.request_tool`` and the
``WorkerManager``).

The base projection (default: last-event-wins) is
applied first; this projection then OVERLAYS the
``tool_requests`` and ``tool_completions`` slots.
The two slot dicts are keyed by ``request_event_id``.

Two entry points:

  - ``project_tool_calls(events)`` -- the original full
    projection: runs a base projection then overlays
    the tool slots. Suitable for ``World.fold``.
  - ``overlay_tool_calls(events, base_views)`` -- the
    overlay-only variant: assumes ``base_views`` is
    already folded and just adds the tool slots.
    Suitable for incremental dispatchers (e.g.
    ``ReactiveDispatcher._fold_with_filter``) that
    already have a post-fold World in hand and
    don't want to pay for a second fold pass.

Replay: given a checkpoint + delta events, refold
with this projection produces the same World. No state
migration needed; components are cache derived.
"""

from __future__ import annotations

import collections
import dataclasses
from collections.abc import Sequence
from typing import Mapping, Optional

from .._typing import JsonValue

from ..event.event import Event
from .components import ToolCallCompletion, ToolCallRequest
from .projection import Projection, project_default
from .view import AgentView


def project_tool_calls(
    events: Sequence[Event],
    *,
    base_projection: Projection = project_default,
) -> dict[str, AgentView]:
    """
    Custom projection: materialise ToolCallRequest and
    ToolCallCompletion components from tool.* events.

    Runs ``base_projection`` first, then overlays the
    tool slots on top. Equivalent to::

        overlay_tool_calls(events, base_projection(events))

    but kept as a separate entry point because most
    callers (e.g. ``World.fold``) want a ``Projection``
    -- a function from events to views, not from
    ``(events, base_views)`` to views.
    """
    base_views = base_projection(events)
    return overlay_tool_calls(events, base_views)


def overlay_tool_calls(
    events: Sequence[Event],
    base_views: Mapping[str, AgentView],
) -> dict[str, AgentView]:
    """
    Overlay-only variant of ``project_tool_calls``.

    Walks ``events`` for tool.* events and adds the
    ``tool_requests`` / ``tool_completions`` slots to
    the corresponding view from ``base_views``. Does
    NOT re-run a base projection -- the caller is
    expected to have already folded the events.

    The output contains:

      - Every agent in ``base_views``, with the
        tool slots installed on top if the batch
        contains tool events for that agent. Agents
        in ``base_views`` with no tool events are
        passed through unchanged (the original view
        is returned as-is, without empty tool slots).
      - Agents with tool events but not in
        ``base_views`` (e.g. an orphan completion for
        an agent not in the fold window) get a fresh
        ``AgentView`` with only the tool slots.

    Contrast with ``project_tool_calls``: the full
    projection always installs empty tool slots on
    every base view, because it produces a complete
    World from a batch. ``overlay_tool_calls`` is
    incremental and only mutates views that need to
    change; agents without tool events are returned
    as their original base view (no allocation, no
    ``dataclasses.replace``).

    Used by ``ReactiveDispatcher._fold_with_filter``
    to enrich the post-fold World without paying for
    a second fold pass (ADR-036 §2.3).
    """
    # Phase 1: walk events to materialise the components.
    tool_requests: dict[str, dict[str, ToolCallRequest]] = collections.defaultdict(dict)
    tool_completions: dict[str, dict[str, ToolCallCompletion]] = (
        collections.defaultdict(dict)
    )
    for e in events:
        request_tool_name = _requested_tool_name(e.event_type)
        if request_tool_name is not None:
            req = _build_request(e, tool_name=request_tool_name)
            tool_requests[e.agent_id][req.request_event_id] = req
        else:
            completion_status = _completion_status(e.event_type)
            if completion_status is not None:
                _maybe_attach_completion(
                    e,
                    tool_requests[e.agent_id],
                    tool_completions[e.agent_id],
                    status=completion_status,
                )

    # Phase 2: build the overlay. Every agent in
    # ``base_views`` is in the output; agents without
    # tool events get their original view unchanged.
    # Agents touched by tool events get the slots
    # installed via ``_overlay``. Agents not in
    # ``base_views`` but touched by tool events get
    # a fresh ``AgentView`` with only the slots.
    out: dict[str, AgentView] = dict(base_views)
    for agent_id in set(tool_requests) | set(tool_completions):
        if agent_id in base_views:
            base_view = base_views[agent_id]
        else:
            base_view = AgentView(agent_id=agent_id)
        out[agent_id] = _overlay(
            base_view,
            requests=tool_requests.get(agent_id, {}),
            completions=tool_completions.get(agent_id, {}),
        )
    return out


def _build_request(event: Event, *, tool_name: str) -> ToolCallRequest:
    """Build a ToolCallRequest from a ``tool.<name>.requested`` event.

    ``tool_name`` is the middle segment of the event type
    (``"weather_api"`` in ``"tool.weather_api.requested"``);
    the caller resolves it via :func:`_requested_tool_name`.
    The legacy bare form (``"tool.requested"``) carries the
    name in ``event.data["tool"]`` instead — handled by
    :func:`_requested_tool_name` returning an empty string
    in that case, which the request then reads from
    ``event.data`` (kept for back-compat with old EventLogs).
    """
    return ToolCallRequest(
        request_event_id=str(event.event_id),
        tool_name=tool_name or str(event.data.get("tool", "")),
        agent_id=event.agent_id,
        params=dict(event.data),
        requested_at=event.timestamp,
        correlation_id=event.correlation.correlation_id,
    )


def _requested_tool_name(event_type: str) -> Optional[str]:
    """
    Resolve the tool name from a request event type.

    Accepts both the bare form (``tool.requested``) and the
    WorkerManager form (``tool.<name>.requested``) emitted
    by ``ToolAwareSystem.request_tool`` (ADR-036 §2.4).
    Returns ``None`` for events that are not a request.
    """
    if event_type == "tool.requested":
        return ""
    if event_type.startswith("tool.") and event_type.endswith(".requested"):
        return event_type[len("tool.") : -len(".requested")]
    return None


def _completion_status(event_type: str) -> Optional[str]:
    """
    Resolve the completion status from an event type.

    Accepts both the bare form (``tool.completed``,
    ``tool.failed``) and the WorkerManager form
    (``tool.<name>.completed``, ``tool.<name>.failed``)
    introduced by ADR-036. Returns ``None`` for events
    that are neither a completion nor a failure.
    """
    if event_type == "tool.completed":
        return "completed"
    if event_type == "tool.failed":
        return "failed"
    if event_type.startswith("tool.") and event_type.endswith(".completed"):
        return "completed"
    if event_type.startswith("tool.") and event_type.endswith(".failed"):
        return "failed"
    return None


def _maybe_attach_completion(
    event: Event,
    requests_for_agent: dict[str, ToolCallRequest],
    completions_for_agent: dict[str, ToolCallCompletion],
    *,
    status: str,
) -> None:
    """Build a ToolCallCompletion and attach it to the
    matching request. The join key is the completion's
    ``causation_id`` (== request's ``event_id``).

    If the request isn't in this agent's dict, the
    completion is silently dropped. This is the
    "orphan completion" case (the request belongs to
    a different agent, or the fold window doesn't
    include it).
    """
    if event.causation_id is None:
        return
    target = str(event.causation_id)
    req = requests_for_agent.get(target)
    if req is None:
        return
    if target in completions_for_agent:
        # A request can only be completed once. A
        # second completion is malformed; drop it.
        return
    completed_at = event.timestamp
    latency_ms = (completed_at - req.requested_at).total_seconds() * 1000.0
    if status == "completed":
        result: Optional[Mapping[str, JsonValue]] = dict(event.data)
        error: Optional[str] = None
    else:
        result = None
        # Failures carry the error in `event.data["error"]`
        # (or `event.data["message"]` as a fallback).
        error = str(event.data.get("error") or event.data.get("message") or "unknown")
    completions_for_agent[target] = ToolCallCompletion(
        request_event_id=target,
        status=status,
        result=result,
        error=error,
        completed_at=completed_at,
        latency_ms=latency_ms,
        correlation_id=event.correlation.correlation_id,
    )


def _overlay(
    base_view: AgentView,
    *,
    requests: dict[str, ToolCallRequest],
    completions: dict[str, ToolCallCompletion],
) -> AgentView:
    """Overlay the tool slots onto the base view.

    The returned view's ``components`` is a plain
    ``dict`` -- the read-only contract is enforced by
    ``AgentView`` being a frozen dataclass (no
    reassignment of ``view.components``). The slot
    values are also plain dicts of frozen dataclasses.
    There is no ``MappingProxyType`` wrapper
    anywhere: it was redundant (frozen already blocks
    external mutation at the field level) and broke
    the World checkpoint's pickle path
    (ADR-036 follow-up).
    """
    components = dict(base_view.components)
    components["tool_requests"] = dict(requests)
    components["tool_completions"] = dict(completions)
    return dataclasses.replace(base_view, components=components)


__all__ = ["overlay_tool_calls", "project_tool_calls"]
