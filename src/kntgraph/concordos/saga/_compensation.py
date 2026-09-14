# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
concordos.saga._compensation -- compensation flow.

Extracted from ``_system.py`` to keep that file under the
500-line guideline (ADR-069 §11.12). The compensation
logic is self-contained: it emits the
``saga.<name>.compensating`` marker, walks the
``compensate_stack`` in LIFO order, and routes a failed
compensation to the DLQ.

Two entry points:

  - ``begin_compensation`` (called from ``_handle_failure``
    and ``_on_saga_timeout``): emits the compensating
    ``tool.<compensate_tool>.requested`` events.
  - ``is_compensation_failure``: predicate that detects
    when a tool-completion trigger is itself a failed
    compensation (the saga is mid-compensation and the
    compensation tool failed).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from ..base import StepContext, ViewTrigger

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.event.event import Event
    from kntgraph.core.world.view import AgentView
    from kntgraph.core.world.world import World
    from kntgraph.concordos.saga._components import SagaProgressComponent
    from kntgraph.concordos.saga._config import SagaConfig, SagaStepConfig


__all__ = ["begin_compensation", "is_compensation_failure", "step_result_payload"]


class _SagaSystemLike(Protocol):
    """Structural type for the saga-system argument.

    The helpers access ``_cfg.name`` and ``_step_map`` on
    the saga system. A ``Protocol`` captures this contract
    without importing ``SagaSystem`` at runtime (which
    would create a circular dependency: ``_system.py``
    imports these helpers).
    """

    @property
    def _cfg(self) -> "SagaConfig": ...
    @property
    def _step_map(self) -> "dict[str, SagaStepConfig]": ...
    def _now(self) -> "object": ...


def begin_compensation(
    saga: _SagaSystemLike,
    world: "World",
    view: "AgentView",
    progress: "SagaProgressComponent",
    trigger: ViewTrigger,
    reason: str,
) -> list["Event"]:
    """
    Emit compensation events in LIFO order.

    For each step on the compensate_stack (the steps that
    already produced an external effect and need to be
    rolled back), we check the step's ``compensate_when``
    Specification. A step whose compensation would be a
    no-op (e.g. a timed-out NF-e emission that never
    landed) is skipped -- see ADR-069 §4.8 example.

    For each dispatched compensation tool, two granular
    events are emitted (ADR-069 §11.10 / §11.18.2):

    - ``saga.<name>.<step>.compensation_started`` -- the
      durable marker that compensation was dispatched.
      The fold projection reads this from the EventLog to
      reconstruct the ``compensate_stack`` accurately
      after a process crash.
    - ``tool.<compensate_tool>.requested`` -- the
      actual tool invocation.

    The ``compensated`` event is emitted by the saga
    system on the matching ``tool.<compensate_tool>.completed``
    (see ``_handle_completion``); the fold projection uses
    it to mark the step as fully compensated.

    If a compensation tool itself fails, the saga emits
    ``saga.<name>.compensation_failed`` and the system
    routes the agent to the DLQ on the next tick
    (ADR-069 §4.5.1).
    """
    from ._records import emit

    out: list[Event] = [
        emit(
            saga,
            trigger,
            event_type=f"saga.{saga._cfg.name}.compensating",
            data={"reason": reason, "saga_id": progress.saga_id},
        )
    ]
    ctx = StepContext(
        step_results=progress.step_results,
        step_states=progress.step_states,
        domain=None,
        continuity=None,
        profile=None,
        agent_id=view.agent_id,
        now=saga._now(),
        cross_agent_resolver=lambda aid: world.views.get(aid),
    )
    for step_name in reversed(progress.compensate_stack):
        step_cfg = saga._step_map.get(step_name)
        if step_cfg is None or step_cfg.compensate_tool is None:
            continue
        if (
            step_cfg.compensate_when is not None
            and not step_cfg.compensate_when.is_satisfied_by(ctx)
        ):
            continue
        # Granular per-step "compensation_started" marker
        # (ADR-069 §11.18.2). The fold projection reads this
        # from the EventLog to reconstruct ``compensate_stack``
        # after a process crash.
        out.append(
            emit(
                saga,
                trigger,
                event_type=(f"saga.{saga._cfg.name}.{step_name}.compensation_started"),
                data={
                    "saga_id": progress.saga_id,
                    "step_name": step_name,
                },
            )
        )
        out.append(
            emit(
                saga,
                trigger,
                event_type=f"tool.{step_cfg.compensate_tool}.requested",
                data={
                    "saga_id": progress.saga_id,
                    "compensating_step": step_name,
                    **step_result_payload(progress, step_name),
                },
            )
        )
    return out


def is_compensation_failure(
    saga: _SagaSystemLike,
    trigger: ViewTrigger,
    progress: "SagaProgressComponent",
) -> bool:
    """True when the trigger is a ``tool.<name>.failed`` event
    for a compensation tool of a step on the compensate_stack
    (i.e. a compensation attempt that itself failed)."""
    if not trigger.event_type.endswith(".failed"):
        return False
    for step_name in progress.compensate_stack:
        step_cfg = saga._step_map.get(step_name)
        if step_cfg is None or step_cfg.compensate_tool is None:
            continue
        if trigger.event_type == f"tool.{step_cfg.compensate_tool}.failed":
            return True
    return False


def step_result_payload(
    progress: "SagaProgressComponent",
    step_name: str,
) -> dict[str, "JsonValue"]:
    """Return the step's result payload (a ``dict[str,
    JsonValue]``) for enrichment, or ``{}`` when the result
    is not a mapping."""
    result = progress.step_results.get(step_name)
    if isinstance(result, Mapping):
        return dict(result)
    return {}
