# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
concordos.saga._records -- event-emission and step-skipping helpers.

Extracted from ``_system.py`` to keep that file under the
500-line guideline (ADR-069 §11.12). These helpers are
called by ``SagaSystem`` (the orchestration layer); each
takes the saga system as its first argument so the
shared state (``_cfg``, ``_step_map``, ``_now``) is
preserved without breaking encapsulation.

Three groups:

  - **Event emission**: ``_emit`` (single low-level
    constructor) and the higher-level wrappers
    (``_saga_completed``, ``_dlq_event``).
  - **Step recorders**: ``_record_start``,
    ``_record_step_completed``, ``_record_step_failed``.
  - **Skip predicates**: ``_first_non_skipped_step``,
    ``_next_non_skipped_step``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..base import StepContext, ViewTrigger

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.clock import Clock
    from kntgraph.core.event.event import Event
    from kntgraph.concordos.saga._components import SagaProgressComponent
    from kntgraph.concordos.saga._config import SagaConfig, SagaStepConfig

    # Imported only for pyright's structural Protocol check:
    # ``SagaSystem`` is the canonical implementation of
    # ``_SagaSystemLike``, but importing it at runtime
    # would create a circular dependency. The
    # ``TYPE_CHECKING`` import gives pyright the full
    # type information it needs to verify that
    # ``SagaSystem`` matches the Protocol.
    from kntgraph.concordos.saga._system import SagaSystem  # noqa: F401


__all__ = [
    "emit",
    "saga_completed",
    "dlq_event",
    "record_start",
    "record_step_completed",
    "record_step_failed",
    "first_non_skipped_step",
    "next_non_skipped_step",
]


@runtime_checkable
class _SagaSystemLike(Protocol):
    """Structural type for the saga-system argument.

    The helper functions in this module access ``_cfg``,
    ``_step_map``, and ``_now`` on the saga system. A
    ``Protocol`` captures this contract without importing
    ``SagaSystem`` at runtime (which would create a
    circular dependency: ``_system.py`` imports these
    helpers).

    Any object with the same shape satisfies the
    structural type; ``SagaSystem`` is one such object.

    The attributes are declared as plain instance
    attributes (not ``@property``) to match the
    ``SagaSystem.__slots__`` layout -- a Protocol with
    ``@property`` would not be structurally assignable
    from a class that exposes the same name as a plain
    attribute.
    """

    _cfg: "SagaConfig"
    _step_map: "Mapping[str, SagaStepConfig]"
    _now: "Clock"


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def emit(
    saga: _SagaSystemLike,
    trigger: ViewTrigger,
    *,
    event_type: str,
    data: "Mapping[str, JsonValue]",
) -> "Event":
    """Low-level event constructor used by all saga helpers.

    Mirrors ``SagaSystem._emit`` but lives at module level so
    the orchestration file stays small. ``saga`` is the
    saga system instance (provides ``_cfg.name`` via
    attribute access).
    """
    from kntgraph.core.event.event import Event

    return Event.create(
        agent_id=trigger.agent_id,
        event_type=event_type,
        event_class="domain",
        data=data,
        causation_id=trigger.event_id,
        correlation=trigger.correlation,
    )


def saga_completed(
    saga: _SagaSystemLike,
    trigger: ViewTrigger,
    progress: "SagaProgressComponent",
) -> "Event":
    """Emit the saga-completed event."""
    return emit(
        saga,
        trigger,
        event_type=f"saga.{saga._cfg.name}.completed",
        data={"saga_id": progress.saga_id},
    )


def dlq_event(
    saga: _SagaSystemLike,
    progress: "SagaProgressComponent",
    trigger: ViewTrigger,
) -> "Event":
    """Build the DLQ-emission domain event for a saga whose
    compensation could not be completed.

    The actual DLQ insertion is performed by an adapter
    system that reads this event and appends to
    ``knt:dlq:saga:<name>``; the saga system only emits the
    typed event so the DLQ adapter stays out of the saga's
    dependency graph.
    """
    return emit(
        saga,
        trigger,
        event_type=f"saga.{saga._cfg.name}.dlq",
        data={
            "saga_id": progress.saga_id,
            "stuck_step": progress.current_step,
            "step_states": dict(progress.step_states),
        },
    )


# ---------------------------------------------------------------------------
# Step recorders
# ---------------------------------------------------------------------------


def record_start(
    saga: _SagaSystemLike,
    trigger: "ViewTrigger",
    progress: "SagaProgressComponent",
    step_config: "SagaStepConfig",
) -> "Event":
    """Record the saga start (the first step is now in flight)."""
    return emit(
        saga,
        trigger,
        event_type=f"saga.{saga._cfg.name}.step_started",
        data={"step_name": step_config.name, "saga_id": progress.saga_id},
    )


def record_step_completed(
    saga: _SagaSystemLike,
    progress: "SagaProgressComponent",
    step_config: "SagaStepConfig",
    trigger: "ViewTrigger",
    new_states: dict,
    new_results: dict,
) -> "Event":
    """Record a step completion, carrying the updated
    step_states / step_results so the next dispatch can
    enrich from them.
    """
    return emit(
        saga,
        trigger,
        event_type=f"saga.{saga._cfg.name}.step_completed",
        data={
            "step_name": step_config.name,
            "saga_id": progress.saga_id,
            "step_states": dict(new_states),
            "step_results": dict(new_results),
        },
    )


def record_step_failed(
    saga: _SagaSystemLike,
    progress: "SagaProgressComponent",
    step_config: "SagaStepConfig",
    trigger: "ViewTrigger",
    new_states: dict,
    new_results: dict,
) -> "Event":
    """Record a step failure, carrying the updated
    step_states / step_results."""
    return emit(
        saga,
        trigger,
        event_type=f"saga.{saga._cfg.name}.step_failed",
        data={
            "step_name": step_config.name,
            "saga_id": progress.saga_id,
            "step_states": dict(new_states),
            "step_results": dict(new_results),
        },
    )


# ---------------------------------------------------------------------------
# Skip predicates (consult the ``skip_when`` Specification)
# ---------------------------------------------------------------------------


def first_non_skipped_step(
    saga: _SagaSystemLike,
    progress: "SagaProgressComponent",
    trigger: "ViewTrigger",
) -> "SagaStepConfig | None":
    """Return the first step in declared order that is not
    skipped (per its ``skip_when`` Specification)."""
    ctx = StepContext(
        step_results=progress.step_results,
        step_states=progress.step_states,
        domain=None,
        continuity=None,
        profile=None,
        agent_id=trigger.agent_id,
        now=saga._now(),
    )
    for step_name in progress.step_order:
        step_cfg = saga._step_map.get(step_name)
        if step_cfg is None:
            continue
        if step_cfg.skip_when is not None and step_cfg.skip_when.is_satisfied_by(ctx):
            continue
        return step_cfg
    return None


def next_non_skipped_step(
    saga: _SagaSystemLike,
    current_step: "SagaStepConfig",
    ctx: StepContext,
) -> "SagaStepConfig | None":
    """Return the next step after ``current_step`` in declared
    order that is not skipped."""
    order = saga._cfg.steps
    try:
        idx = order.index(current_step)
    except ValueError:
        return None
    for step_cfg in order[idx + 1 :]:
        if step_cfg.skip_when is not None and step_cfg.skip_when.is_satisfied_by(ctx):
            continue
        return step_cfg
    return None
