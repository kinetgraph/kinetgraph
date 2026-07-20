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
from datetime import timedelta
from typing import Mapping, Optional

from .._typing import JsonValue

from ..event.event import Event
from .components import ToolCallCompletion, ToolCallRequest, ToolCallTTL
from .projection import Projection, project_default
from .view import AgentView


def project_tool_calls(
    events: Sequence[Event],
    *,
    base_projection: Projection = project_default,
    ttl: ToolCallTTL = ToolCallTTL(),
) -> dict[str, AgentView]:
    """
    Custom projection: materialise ToolCallRequest and
    ToolCallCompletion components from tool.* events.

    Runs ``base_projection`` first, then overlays the
    tool slots on top. Equivalent to::

        overlay_tool_calls(events, base_projection(events),
                           ttl=ttl)

    but kept as a separate entry point because most
    callers (e.g. ``World.fold``) want a ``Projection``
    -- a function from events to views, not from
    ``(events, base_views)`` to views.

    The ``ttl`` argument (ADR-045) defaults to
    ``ToolCallTTL()`` (5-minute global TTL). The TTL
    is **set** on each new request (``expires_at =
    requested_at + ttl_seconds``) but is **not
    enforced** by the projection; the
    :class:`ToolCallTTLSweeperSystem` (a separate
    ``WorldSystem`` registered with the dispatcher)
    emits ``tool.<name>.failed`` events for stale
    requests. The separation keeps the projection
    pure (no clock injection, no I/O).

    **Back-compat note**: callers that pre-date
    ADR-045 (and use the default ``ToolCallTTL()``)
    get the new ``expires_at`` field set on each
    request automatically. The TTL itself is
    enforced by the sweeper system. The
    ``overlay_tool_calls`` function used by the
    dispatcher accepts the same args; see
    ``ReactiveDispatcher(tool_ttls=...)``.
    """
    base_views = base_projection(events)
    return overlay_tool_calls(events, base_views, ttl=ttl)


def overlay_tool_calls(
    events: Sequence[Event],
    base_views: Mapping[str, AgentView],
    *,
    ttl: ToolCallTTL = ToolCallTTL(),
    post_systems: bool = False,
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

    ``ttl`` (ADR-045): the overlay SETS the
    ``expires_at`` field on each new request (via
    ``_build_request``), but does NOT enforce the
    TTL itself. The TTL is enforced by the
    :class:`ToolCallTTLSweeperSystem` (a separate
    ``WorldSystem`` registered with the
    dispatcher); the sweeper emits
    ``tool.<name>.failed`` events for stale
    requests. The separation keeps the overlay
    pure (no clock injection) and the TTL
    enforcement explicit (a system that downstream
    consumers can observe).
    """
    # Phase 1: walk events to materialise the components.
    # ADR-044: we accumulate the new requests/completions
    # from this batch and merge with the existing slots
    # from ``base_views`` (which carry the state from
    # previous ticks). This way the overlay is incremental:
    # a request emitted in tick N remains visible in the
    # slot in tick N+1 even if the current batch has only
    # a completion event.
    #
    # ADR-045: the per-tool TTL is set on each new
    # request (``expires_at = requested_at +
    # ttl_seconds``). The TTL itself is NOT enforced
    # here; the
    # :class:`ToolCallTTLSweeperSystem` emits a
    # ``tool.<name>.failed`` event when ``now >=
    # expires_at``. Keeping the overlay pure (no
    # clock injection, no I/O) preserves the
    # framework's projection-as-data invariant.
    tool_requests: dict[str, dict[str, ToolCallRequest]] = collections.defaultdict(dict)
    tool_completions: dict[str, dict[str, ToolCallCompletion]] = (
        collections.defaultdict(dict)
    )
    agents_to_merge: set[str] = set()
    for e in events:
        request_tool_name = _requested_tool_name(e.event_type)
        if request_tool_name is not None:
            _collect_request(
                e,
                tool_name=request_tool_name,
                ttl=ttl,
                tool_requests=tool_requests,
                agents_to_merge=agents_to_merge,
            )
        else:
            completion_status = _completion_status(e.event_type)
            if completion_status is not None:
                _collect_completion(
                    e,
                    base_views=base_views,
                    status=completion_status,
                    tool_requests=tool_requests,
                    tool_completions=tool_completions,
                    agents_to_merge=agents_to_merge,
                )

    return _assemble_overlay(
        base_views,
        tool_requests=tool_requests,
        tool_completions=tool_completions,
        agents_to_merge=agents_to_merge,
        post_systems=post_systems,
    )


def _collect_request(
    e: Event,
    *,
    tool_name: str,
    ttl: "ToolCallTTL",
    tool_requests: dict[str, dict[str, "ToolCallRequest"]],
    agents_to_merge: set[str],
) -> None:
    """Handle a single ``tool.<name>.requested`` event:
    build a ``ToolCallRequest`` (with the per-tool TTL
    applied) and register the request in the
    ``tool_requests`` map. The request is the "new"
    side of the overlay; it is merged with the base
    view's existing requests in ``_assemble_overlay``."""
    ttl_seconds = ttl.ttl_for(tool_name)
    req = _build_request(e, tool_name=tool_name, ttl_seconds=ttl_seconds)
    tool_requests[e.agent_id][req.request_event_id] = req
    agents_to_merge.add(e.agent_id)


def _collect_completion(
    e: Event,
    *,
    base_views: Mapping[str, "AgentView"],
    status: str,
    tool_requests: dict[str, dict[str, "ToolCallRequest"]],
    tool_completions: dict[str, dict[str, "ToolCallCompletion"]],
    agents_to_merge: set[str],
) -> None:
    """Handle a single ``tool.<name>.completed`` /
    ``tool.<name>.failed`` event: build a
    ``ToolCallCompletion`` and attach it to the
    matching request. The join key is the completion's
    ``causation_id`` (== request's ``event_id``). The
    request may live in EITHER the local batch
    (``tool_requests``) OR the base view (a previous
    tick's request). Look in both."""
    existing_requests_for_agent: dict[str, ToolCallRequest] = base_views.get(
        e.agent_id, AgentView(agent_id=e.agent_id)
    ).components.get("tool_requests", {})
    _maybe_attach_completion(
        e,
        requests_for_agent={
            **existing_requests_for_agent,
            **tool_requests[e.agent_id],
        },
        completions_for_agent=tool_completions[e.agent_id],
        status=status,
    )
    agents_to_merge.add(e.agent_id)


def _assemble_overlay(
    base_views: Mapping[str, "AgentView"],
    *,
    tool_requests: dict[str, dict[str, "ToolCallRequest"]],
    tool_completions: dict[str, dict[str, "ToolCallCompletion"]],
    agents_to_merge: set[str],
    post_systems: bool,
) -> dict[str, "AgentView"]:
    """Build the overlay map: every agent in
    ``base_views`` is in the output; agents without
    tool events get their original view unchanged.
    Agents touched by tool events get the slots
    installed via ``_overlay``. Agents not in
    ``base_views`` but touched by tool events get a
    fresh ``AgentView`` with only the slots.

    Accumulation (ADR-044): the new
    ``tool_requests`` / ``tool_completions`` dicts
    are MERGED with the existing slots on the base
    view (if any), keyed by ``request_event_id``.
    A request emitted in tick N remains visible in
    tick N+K; a completion overwrites the matching
    entry (the framework's at-least-once delivery
    contract).
    """
    out: dict[str, AgentView] = dict(base_views)
    for agent_id in agents_to_merge:
        out[agent_id] = _build_overlay_view(
            base_views.get(agent_id, AgentView(agent_id=agent_id)),
            tool_requests.get(agent_id, {}),
            tool_completions.get(agent_id, {}),
            post_systems=post_systems,
        )
    return out


def _build_overlay_view(
    base_view: "AgentView",
    new_requests: dict[str, "ToolCallRequest"],
    new_completions: dict[str, "ToolCallCompletion"],
    *,
    post_systems: bool,
) -> "AgentView":
    """Install the (merged) tool slots on the base
    view. The merge is keyed by ``request_event_id``;
    the new batch wins (it is the most recent
    observation). The eviction policy (ADR-044
    §2.3 option 1) drops a request from the slot
    when a matching completion exists, but ONLY when
    the request was carried in from a previous tick
    (``existing_requests``). Requests created by the
    current batch are kept (the system may not have
    reacted to them yet). The ``post_systems`` flag
    is set by the dispatcher's second fold pass
    (after the systems have run) so the eviction
    applies unconditionally — the system has had its
    chance to react."""
    existing_requests: dict[str, ToolCallRequest] = base_view.components.get(
        "tool_requests", {}
    )
    existing_completions: dict[str, ToolCallCompletion] = base_view.components.get(
        "tool_completions", {}
    )
    merged_requests = {**existing_requests, **new_requests}
    merged_completions = {**existing_completions, **new_completions}
    for request_id in list(merged_requests.keys()):
        if request_id in merged_completions:
            if post_systems or (
                request_id in existing_requests and request_id in existing_completions
            ):
                merged_requests.pop(request_id, None)
                merged_completions.pop(request_id, None)
    return _overlay(
        base_view,
        requests=merged_requests,
        completions=merged_completions,
    )


def _build_request(
    event: Event, *, tool_name: str, ttl_seconds: float
) -> ToolCallRequest:
    """Build a ToolCallRequest from a ``tool.<name>.requested`` event.

    ``tool_name`` is the middle segment of the event type
    (``"weather_api"`` in ``"tool.weather_api.requested"``);
    the caller resolves it via :func:`_requested_tool_name`.
    The legacy bare form (``"tool.requested"``) carries the
    name in ``event.data["tool"]`` instead — handled by
    :func:`_requested_tool_name` returning an empty string
    in that case, which the request then reads from
    ``event.data`` (kept for back-compat with old EventLogs).

    ``ttl_seconds`` (ADR-045): the TTL for the request,
    in seconds. The ``expires_at`` field is computed as
    ``requested_at + timedelta(seconds=ttl_seconds)``;
    a TTL of ``0`` (or negative) means **TTL disabled**
    (``expires_at = None``). The
    :class:`ToolCallTTLSweeperSystem` emits a
    ``tool.<name>.failed`` event when
    ``now >= expires_at``.
    """
    requested_at = event.timestamp
    if ttl_seconds > 0:
        expires_at = requested_at + timedelta(seconds=ttl_seconds)
    else:
        expires_at = None
    return ToolCallRequest(
        request_event_id=str(event.event_id),
        tool_name=tool_name or str(event.data.get("tool", "")),
        agent_id=event.agent_id,
        params=dict(event.data),
        requested_at=requested_at,
        correlation_id=event.correlation.correlation_id,
        expires_at=expires_at,
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
