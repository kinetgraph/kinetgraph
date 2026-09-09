# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.saga._state -- saga progress reconciliation (ADR-069 §9.2 item 6).

The ``SagaProgressComponent`` carries two kinds of fields:

  - *execution* fields (``saga_id``, ``saga_name``,
    ``current_step``, ``direction``, ``started_at``) — written
    by the saga events.
  - *history* fields (``step_states``, ``step_results``,
    ``compensate_stack``) — derived from the EventLog.

This module provides the **reconciliation** that re-derives the
component deterministically from a batch of saga events, so a
re-fold of the EventLog reconstructs the same progress without
relying on an in-memory cache. It is the projection function the
ADR-069 §11.10 resolution names.

The projection is a :class:`WorldProjection` (ADR-069 §11.10):
pure, composed into the dispatcher fold after the base fold and
before the tool overlay. It needs the ``SagaConfig`` to know the
declared ``step_order`` and which steps carry a
``compensate_tool`` (both are config-derived, not event-derived).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import Any

from kntgraph.core.event import Event
from kntgraph.core.world.view import AgentView
from kntgraph.core.world.world import World

from ._components import SagaProgressComponent
from ._config import SagaConfig

__all__ = ["SagaProjection", "reconcile_saga_progress"]


def _saga_event_types(config: SagaConfig) -> frozenset[str]:
    """The set of event types the saga emits (the projection's
    trigger set). The ``saga.<name>.*`` namespace is owned by the
    saga; the projection only reacts to it."""
    name = config.name
    return frozenset(
        {
            f"saga.{name}.started",
            f"saga.{name}.step_started",
            f"saga.{name}.step_completed",
            f"saga.{name}.step_failed",
            f"saga.{name}.compensating",
            f"saga.{name}.completed",
            f"saga.{name}.dlq",
            f"saga.{name}.timed_out",
            f"saga.{name}.compensation_failed",
        }
    )


def _init_state(config: SagaConfig) -> dict[str, Any]:
    """Initialise the fold's mutable state from the config."""
    return {
        "saga_id": "",
        "saga_name": config.name,
        "current_step": "",
        "direction": "forward",
        "step_states": {},
        "step_results": {},
        "compensate_stack": [],
        "started_at": None,
        "awaiting_approval_at": {},
    }


def _on_started(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.started``: begin the saga in ``forward``."""
    state["saga_id"] = str(e.data.get("saga_id", state["saga_id"]))
    state["direction"] = "forward"
    state["started_at"] = e.timestamp
    # The first step is not yet dispatched; the SagaSystem
    # dispatches it on the next tick. ``current_step`` stays
    # empty until ``step_started`` lands.


def _on_step_started(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.step_started``: the named step is in flight."""
    step_name = str(e.data.get("step_name", ""))
    if step_name:
        state["current_step"] = step_name
        state["step_states"][step_name] = "in_flight"
        # A human step (``tool_name is None``) is not dispatched
        # via a tool; it enters ``awaiting_approval``. Record the
        # timestamp so the per-step approval timeout (ADR-069 §9.2
        # item 3) can detect a stalled approval.
        step_cfg = next((s for s in config.steps if s.name == step_name), None)
        if step_cfg is not None and step_cfg.tool_name is None:
            state["awaiting_approval_at"][step_name] = e.timestamp


def _on_awaiting_approval(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.<step>.awaiting_approval``: a human step is
    waiting for external approval. Record the timestamp."""
    step_name = str(e.data.get("step_name", ""))
    if step_name:
        state["current_step"] = step_name
        state["step_states"][step_name] = "awaiting_approval"
        state["awaiting_approval_at"][step_name] = e.timestamp


def _on_step_completed(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.step_completed``: carry the updated
    step_states / step_results (the saga system stamps them on
    the event)."""
    _apply_step_snapshot(e, state)


def _on_step_failed(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.step_failed``: carry the updated
    step_states / step_results."""
    _apply_step_snapshot(e, state)


def _apply_step_snapshot(e: Event, state: dict[str, Any]) -> None:
    """Merge the ``step_states`` / ``step_results`` snapshots the
    saga system stamps on ``step_completed`` / ``step_failed``."""
    states = e.data.get("step_states")
    if isinstance(states, Mapping):
        state["step_states"] = dict(states)
    results = e.data.get("step_results")
    if isinstance(results, Mapping):
        state["step_results"] = dict(results)


def _on_compensating(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.compensating``: the saga is rolling back."""
    state["direction"] = "compensating"
    # The compensate_stack is the set of steps that completed and
    # carry a compensate_tool (config-derived), in LIFO order.
    state["compensate_stack"] = [
        s.name
        for s in reversed(config.steps)
        if s.compensate_tool is not None
        and state["step_states"].get(s.name) == "completed"
    ]


def _on_completed(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.completed``: the saga finished forward."""
    state["direction"] = "done"


def _on_dlq(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.dlq``: compensation could not finish."""
    state["direction"] = "compensation_failed"
    stuck = e.data.get("stuck_step")
    if stuck:
        state["current_step"] = str(stuck)


def _on_timed_out(e: Event, state: dict[str, Any], config: SagaConfig) -> None:
    """``saga.<name>.timed_out``: the saga exceeded its deadline."""
    stuck = e.data.get("stuck_at_step")
    if stuck:
        state["current_step"] = str(stuck)


def _on_compensation_failed(
    e: Event, state: dict[str, Any], config: SagaConfig
) -> None:
    """``saga.<name>.compensation_failed``: a compensation tool
    failed; the saga routes to the DLQ."""
    state["direction"] = "compensation_failed"
    stuck = e.data.get("stuck_step")
    if stuck:
        state["current_step"] = str(stuck)


_HANDLERS: dict[str, Any] = {
    "started": _on_started,
    "step_started": _on_step_started,
    "step_completed": _on_step_completed,
    "step_failed": _on_step_failed,
    "compensating": _on_compensating,
    "completed": _on_completed,
    "dlq": _on_dlq,
    "timed_out": _on_timed_out,
    "compensation_failed": _on_compensation_failed,
}


def _fold_agent(
    config: SagaConfig,
    agent_events: Sequence[Event],
    base: SagaProgressComponent | None,
) -> SagaProgressComponent | None:
    """Fold a batch of saga events into a ``SagaProgressComponent``.

    Returns ``None`` when the batch has no saga event for this
    agent AND the base view has no saga component (the caller
    keeps the base view unchanged — no allocation).
    """
    state = _init_state(config)
    if base is not None:
        state["saga_id"] = base.saga_id
        state["current_step"] = base.current_step
        state["direction"] = base.direction
        state["step_states"] = dict(base.step_states)
        state["step_results"] = dict(base.step_results)
        state["compensate_stack"] = list(base.compensate_stack)
        state["started_at"] = base.started_at
        state["awaiting_approval_at"] = dict(base.awaiting_approval_at)

    prefix = f"saga.{config.name}."
    saw_event = False
    for e in agent_events:
        if not e.event_type.startswith(prefix):
            continue
        suffix = e.event_type[len(prefix) :]
        handler = _HANDLERS.get(suffix)
        if handler is None:
            # ``saga.<name>.<step>.awaiting_approval`` — the step
            # name is dynamic, so the suffix is ``<step>.awaiting_approval``.
            if suffix.endswith(".awaiting_approval"):
                handler = _on_awaiting_approval
            else:
                continue
        saw_event = True
        handler(e, state, config)

    if not saw_event and base is None:
        return None
    if not state["saga_id"]:
        return None
    return _build_component(config, state)


def _build_component(
    config: SagaConfig, state: dict[str, Any]
) -> SagaProgressComponent:
    """Materialise the ``SagaProgressComponent`` from the fold
    state."""
    return SagaProgressComponent(
        saga_id=state["saga_id"],
        saga_name=config.name,
        current_step=state["current_step"],
        direction=state["direction"],
        step_order=tuple(s.name for s in config.steps),
        step_states=MappingProxyType(dict(state["step_states"])),
        step_results=MappingProxyType(dict(state["step_results"])),
        compensate_stack=tuple(state["compensate_stack"]),
        started_at=state["started_at"] or datetime.min,
        awaiting_approval_at=MappingProxyType(dict(state["awaiting_approval_at"])),
    )


def reconcile_saga_progress(
    config: SagaConfig,
    events: Sequence[Event],
    base_views: "Mapping[str, AgentView]",
) -> dict[str, AgentView]:
    """Pure fold: saga events → AgentView with the
    ``SagaProgressComponent`` installed on the relevant agents.

    Mirrors ``project_memory`` (ADR-042 §6.1): the returned dict
    mirrors the input (every base view is in the output) and
    overlays the saga component on agents whose events included a
    saga event. Agents with no saga event are passed through
    unchanged (no allocation).
    """
    out: dict[str, AgentView] = dict(base_views)
    for agent_id, view in base_views.items():
        base = view.get_component(SagaProgressComponent)
        agent_events = [e for e in events if e.agent_id == agent_id]
        updated = _fold_agent(config, agent_events, base)
        if updated is None:
            continue
        new_components: dict[Any, Any] = dict(view.components)
        new_components[SagaProgressComponent] = updated
        out[agent_id] = replace(view, components=new_components)
    return out


class SagaProjection:
    """A :class:`WorldProjection` that materialises
    ``SagaProgressComponent`` from saga events (ADR-069 §9.2 item 6).

    Composed into the dispatcher fold after the base fold and
    before the tool overlay. Pure: same ``(world, events)`` ⇒ same
    ``World``.
    """

    __slots__ = ("_config",)

    def __init__(self, config: SagaConfig) -> None:
        self._config = config

    def __call__(self, world: "World", events: list[Event]) -> "World":
        new_views = reconcile_saga_progress(self._config, events, world.views)
        if not new_views:
            return world
        changed: bool = False
        new_storage = world.storage
        for agent_id, projected_view in new_views.items():
            if world.views.get(agent_id) is projected_view:
                continue
            new_storage = new_storage.clone_with_entity(
                agent_id,
                dict(projected_view.components),
            )
            changed = True
        if not changed:
            return world
        return World(tick=world.tick, storage=new_storage, views=new_views)
