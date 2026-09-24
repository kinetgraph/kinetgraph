# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
concordos.saga._system -- WorkflowSaga WorldSystem (ADR-069 §4.5).

``SagaSystem`` orchestrates a sequence of tool calls with
context enrichment, skip conditions, failure policies, and
compensation. It is built entirely on top of existing
framework primitives (ADR-034 tool calls, ADR-045 TTL,
ADR-042 memory) and does NOT reinvent the tool call
lifecycle.

The orchestration logic is split across four modules
(ADR-069 §11.12 -- 500-line guideline):

  - ``_system.py``     -- this file: trigger dispatch
                           and saga lifecycle (``__call__``,
                           ``_events_for_agent``,
                           ``_build_trigger``,
                           ``_handle_completion``,
                           ``_advance``, ``_handle_failure``).
  - ``_dispatch.py``   -- per-step dispatch and param
                           enrichment (``dispatch_step``,
                           ``enrich_params``).
  - ``_compensation.py`` -- compensation flow
                           (``begin_compensation``,
                           ``is_compensation_failure``).
  - ``_records.py``    -- event emission, step recorders,
                           skip predicates, DLQ event.

The system is pure: same ``World`` ⇒ same ``list[Event]``.
``now`` is injected (defaults to the framework clock) so a
replayed log re-evaluates guards / timeouts with the same
timestamp.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from uuid import UUID

from kntgraph.core.clock import injectable_clock
from kntgraph.core.components.memory import (
    ContinuityComponent,
    ProfileComponent,
)
from kntgraph.core.event.correlation import correlation_middleware
from kntgraph.core.world.component import DomainComponent
from kntgraph.core.world.components import ToolCallCompletion, ToolCallRequest
from kntgraph.core.world.world import World

from ..base import StepContext, ViewTrigger
from ._components import SagaProgressComponent
from ._config import SagaConfig, SagaStepConfig

if TYPE_CHECKING:
    from kntgraph.core.clock import Clock
    from kntgraph.core.event.event import Event
    from kntgraph.core.world.view import AgentView
    from ._records import _SagaSystemLike

__all__ = ["SagaSystem"]


def _saga_self(self: "SagaSystem") -> "_SagaSystemLike":
    """Cast ``self`` to ``_SagaSystemLike`` for helper calls.

    Runtime conformance is guaranteed by ``SagaSystem.__slots__``
    matching the Protocol attributes exactly. The cast exists
    because pyright cannot verify the structural match across
    the ``_records`` / ``_system`` import boundary: that
    boundary is needed to avoid a runtime circular dependency
    (``_system`` imports the helpers; the helpers' Protocol
    must not import ``_system``).
    """
    return cast("_SagaSystemLike", self)


class SagaSystem:
    """
    C-02: WorkflowSaga -- WorldSystem (post-ADR-018).

    Reads from the post-fold ``World``. For every agent whose
    archetype carries a ``SagaProgressComponent``, walks the
    agent's recent events and reacts to:

    - ``saga.<name>.started``             → dispatch first step
    - ``tool.<name>.completed``           → advance or compensate
    - ``tool.<name>.failed``              → evaluate fail_when; compensate
    - ``tool.<name>.timed_out``           → treat as failed (ADR-045)
    - ``saga.<name>.timed_out``           → force compensation
                                            (SagaTimeoutSystem)
    - ``saga.<name>.compensation_failed`` → DLQ + alert (§4.5.1)

    Reads ``ToolCallCompletion`` from the ``tool_completions``
    slot (already materialised by ``project_tool_calls``,
    ADR-034). Does NOT re-implement in-flight or resolution
    tracking.
    """

    # Class-level annotations so pyright can verify the
    # structural ``_SagaSystemLike`` Protocol match (the
    # ``__slots__`` tuple below carries no type info).
    # The annotations use ``Mapping`` (not ``dict``) to
    # match the Protocol exactly -- structural matching
    # is invariant on the declared type.
    _cfg: "SagaConfig"
    _step_map: "Mapping[str, SagaStepConfig]"
    _now: "Clock"

    __slots__ = ("_cfg", "_step_map", "_now")

    def __init__(
        self,
        config: "SagaConfig",
        *,
        now: "Clock | None" = None,
    ) -> None:
        self._cfg = config
        self._step_map = {s.name: s for s in config.steps}
        self._now = injectable_clock(now)

    def __call__(self, world: "World") -> list["Event"]:
        out: list[Event] = []
        for _agent_id, view in world.query_agents(SagaProgressComponent):
            out.extend(self._events_for_agent(view, world))
        return out

    def _events_for_agent(self, view: "AgentView", world: "World") -> list["Event"]:
        saga = view.get_component(SagaProgressComponent)
        if saga is None:
            return []

        # The trigger is derived from the view, exactly as the
        # FSM does (§3.4): ``view.domain_phase`` is the last
        # domain event's type, ``view.last_event_id`` its id,
        # and ``view.components[trigger_type]`` its data (the
        # default fold installs the event payload under a
        # component keyed by the event_type). Correlation comes
        # from the middleware (non-None inside a tick, ADR-037).
        # No ``view.last_event`` envelope is required (ADR-069
        # §11.16).
        trigger = self._build_trigger(view)
        if trigger is None:
            return []

        # Saga start
        if trigger.event_type == f"saga.{self._cfg.name}.started":
            return self._start(view, trigger, saga)

        # Saga-level timeout (from SagaTimeoutSystem)
        if trigger.event_type == f"saga.{self._cfg.name}.timed_out":
            return self._on_saga_timeout(world, view, saga, trigger)

        # Compensation failure (§4.5.1): escalate to DLQ.
        if trigger.event_type == f"saga.{self._cfg.name}.compensation_failed":
            from ._records import dlq_event

            return [dlq_event(_saga_self(self), saga, trigger)]

        # A compensation tool that itself fails while the saga is
        # compensating (§4.5.1): emit ``compensation_failed`` then
        # ``dlq`` so the operator can intervene.
        if saga.direction == "compensating" and self._is_compensation_failure(
            trigger, saga
        ):
            return self._on_compensation_failure(saga, trigger)

        # Tool completion / failure / timeout
        if not self._is_tool_trigger(trigger):
            return []

        step_config = self._match_step(view, saga)
        if step_config is None:
            return []

        return self._handle_completion(view, world, saga, step_config, trigger)

    def _build_trigger(self, view: "AgentView") -> "ViewTrigger | None":
        """Derive the trigger from the view's existing fields
        (ADR-069 §11.16). ``None`` when the agent has no domain
        event yet."""
        trigger_type = view.domain_phase
        if trigger_type is None:
            return None
        return ViewTrigger(
            agent_id=view.agent_id,
            event_type=trigger_type,
            event_id=UUID(str(view.last_event_id))
            if view.last_event_id is not None
            else None,
            data=view.components.get(trigger_type, {}),
            correlation=correlation_middleware.current(),
        )

    def _on_saga_timeout(
        self,
        world: "World",
        view: "AgentView",
        saga: SagaProgressComponent,
        trigger: "ViewTrigger",
    ) -> list["Event"]:
        """Handle a saga-level timeout: begin compensation when
        the saga is still moving forward, otherwise ignore."""
        if saga.direction == "forward":
            from ._compensation import begin_compensation

            return begin_compensation(
                _saga_self(self), world, view, saga, trigger, reason="saga_timeout"
            )
        return []

    def _on_compensation_failure(
        self,
        saga: SagaProgressComponent,
        trigger: "ViewTrigger",
    ) -> list["Event"]:
        """Emit ``compensation_failed`` then ``dlq`` so the
        operator can intervene (§4.5.1)."""
        from ._records import dlq_event, emit

        return [
            emit(
                _saga_self(self),
                trigger,
                event_type=f"saga.{self._cfg.name}.compensation_failed",
                data={
                    "saga_id": saga.saga_id,
                    "stuck_step": saga.current_step,
                },
            ),
            dlq_event(_saga_self(self), saga, trigger),
        ]

    def _is_tool_trigger(self, trigger: "ViewTrigger") -> bool:
        """True when the trigger is a tool completion / failure /
        timeout event."""
        return trigger.event_type.startswith("tool.") and trigger.event_type.endswith(
            (".completed", ".failed", ".timed_out")
        )

    # ------------------------------------------------------------------
    # Step matching (join key: saga ↔ tool completion).
    # ------------------------------------------------------------------
    def _match_step(
        self,
        view: "AgentView",
        saga: SagaProgressComponent,
    ) -> "SagaStepConfig | None":
        """
        Find the saga step that the incoming tool-completion
        trigger belongs to.

        The join key is the step currently in flight
        (``saga.current_step``): the completion whose
        ``tool_name`` matches that step's ``tool_name`` in the
        ``tool_completions`` slot (ADR-034). If the completion
        is not in the slot (it has not yet been folded) the
        system emits no events; the next tick will re-run and
        pick it up. This is idempotent.
        """
        step_cfg = self._step_map.get(saga.current_step)
        if step_cfg is None:
            return None
        if self._completion_for_step(view, step_cfg) is None:
            return None
        return step_cfg

    def _is_compensation_failure(
        self,
        trigger: "ViewTrigger",
        saga: SagaProgressComponent,
    ) -> bool:
        """True when the trigger is a ``tool.<name>.failed``
        event for a compensation tool of a step on the
        compensate_stack (i.e. a compensation attempt that
        itself failed)."""
        from ._compensation import is_compensation_failure

        return is_compensation_failure(_saga_self(self), trigger, saga)

    def _completion_for_step(
        self,
        view: "AgentView",
        step_config: "SagaStepConfig",
    ) -> "ToolCallCompletion | None":
        """Return the ``ToolCallCompletion`` for the step's tool.

        The join uses the ``tool_requests`` slot (ADR-034): the
        ``ToolCallRequest`` carries the ``tool_name`` and the
        ``request_event_id``; the ``ToolCallCompletion`` is keyed
        by that same ``request_event_id`` in the
        ``tool_completions`` slot. ``None`` when the completion
        has not yet been folded (the next tick re-runs and picks
        it up).
        """
        if step_config.tool_name is None:
            return None
        requests: "Mapping[str, ToolCallRequest]" = view.components.get(
            "tool_requests", {}
        )
        completions: "Mapping[str, ToolCallCompletion]" = view.components.get(
            "tool_completions", {}
        )
        for request in requests.values():
            if request.tool_name == step_config.tool_name:
                return completions.get(request.request_event_id)
        return None

    # ------------------------------------------------------------------
    # Saga lifecycle: start / handle_completion / advance /
    # handle_failure.
    # ------------------------------------------------------------------
    def _start(
        self,
        view: "AgentView",
        trigger: "ViewTrigger",
        saga: SagaProgressComponent,
    ) -> list["Event"]:
        """Dispatch the first non-skipped step."""
        from ._dispatch import dispatch_step
        from ._records import first_non_skipped_step, record_start, saga_completed

        step_config = first_non_skipped_step(_saga_self(self), saga, trigger)
        if step_config is None:
            # All steps skipped: saga completes immediately
            return [saga_completed(_saga_self(self), trigger, saga)]
        return [
            record_start(_saga_self(self), trigger, saga, step_config),
            dispatch_step(_saga_self(self), view, step_config, trigger),
        ]

    def _handle_completion(
        self,
        view: "AgentView",
        world: "World",
        saga: SagaProgressComponent,
        step_config: "SagaStepConfig",
        trigger: "ViewTrigger",
    ) -> list["Event"]:
        status = trigger.event_type.rsplit(".", 1)[-1]
        # ToolCallCompletion already in AgentView (ADR-034).
        # The completion for this step's dispatch is found by
        # matching the step's tool_name against the
        # ``tool_completions`` slot (see ``_match_step``).
        completion = self._completion_for_step(view, step_config)
        result: dict = dict(completion.result or {}) if completion else {}

        new_states = dict(saga.step_states)
        new_states[step_config.name] = status

        new_results = dict(saga.step_results)
        new_results[step_config.name] = result

        ctx = StepContext(
            step_results=MappingProxyType(new_results),
            step_states=MappingProxyType(new_states),
            domain=view.get_component(DomainComponent),
            continuity=view.get_component(ContinuityComponent),
            profile=view.get_component(ProfileComponent),
            agent_id=view.agent_id,
            now=self._now(),
            cross_agent_resolver=lambda aid: world.views.get(aid),
        )

        # Check proceed_when on success
        if status == "completed":
            if (
                step_config.proceed_when is not None
                and not step_config.proceed_when.is_satisfied_by(ctx)
            ):
                # Treat as failure: proceed condition not met
                new_states[step_config.name] = "failed"
                ctx = dataclasses.replace(
                    ctx,
                    step_states=MappingProxyType(new_states),
                )
                return self._handle_failure(
                    world,
                    view,
                    saga,
                    step_config,
                    trigger,
                    ctx,
                    new_states,
                    new_results,
                )
            return self._advance(
                view, saga, step_config, trigger, ctx, new_states, new_results
            )

        # Failure or timeout
        return self._handle_failure(
            world,
            view,
            saga,
            step_config,
            trigger,
            ctx,
            new_states,
            new_results,
        )

    def _advance(
        self,
        view: "AgentView",
        saga: SagaProgressComponent,
        current_step: "SagaStepConfig",
        trigger: "ViewTrigger",
        ctx: StepContext,
        new_states: dict,
        new_results: dict,
    ) -> list["Event"]:
        """Move to the next non-skipped step or complete the saga."""
        from ._dispatch import dispatch_step
        from ._records import (
            next_non_skipped_step,
            record_start,
            record_step_completed,
            saga_completed,
        )

        next_step = next_non_skipped_step(_saga_self(self), current_step, ctx)
        record = record_step_completed(
            _saga_self(self), saga, current_step, trigger, new_states, new_results
        )
        if next_step is None:
            return [record, saga_completed(_saga_self(self), trigger, saga)]
        # ``record_start`` emits ``saga.<name>.<step>.step_started``,
        # which the ``SagaProjection`` reads to advance
        # ``current_step``. Without it the next tick's
        # ``_match_step`` still looks for the just-completed
        # step's completion and returns None, deadlocking the
        # saga (the tool completion for the new step is in
        # the slot but the saga is still pinned to the
        # previous one).
        step_started = record_start(_saga_self(self), trigger, saga, next_step)
        return [
            record,
            step_started,
            dispatch_step(_saga_self(self), view, next_step, trigger),
        ]

    def _handle_failure(
        self,
        world: "World",
        view: "AgentView",
        saga: SagaProgressComponent,
        step_config: "SagaStepConfig",
        trigger: "ViewTrigger",
        ctx: StepContext,
        new_states: dict,
        new_results: dict,
    ) -> list["Event"]:
        """Evaluate fail_when; begin compensation or continue."""
        from ._compensation import begin_compensation
        from ._dispatch import dispatch_step
        from ._records import (
            next_non_skipped_step,
            record_start,
            record_step_failed,
            saga_completed,
        )

        fail_spec = self._cfg.fail_when
        should_fail = (
            fail_spec.is_satisfied_by(ctx)
            if fail_spec is not None
            else True  # default: fail on first failure
        )
        record = record_step_failed(
            _saga_self(self), saga, step_config, trigger, new_states, new_results
        )
        if should_fail:
            return [record] + begin_compensation(
                _saga_self(self), world, view, saga, trigger, reason="step_failure"
            )
        # continue to next step despite this step's failure
        next_step = next_non_skipped_step(_saga_self(self), step_config, ctx)
        if next_step is None:
            return [record, saga_completed(_saga_self(self), trigger, saga)]
        step_started = record_start(_saga_self(self), trigger, saga, next_step)
        return [
            record,
            step_started,
            dispatch_step(_saga_self(self), view, next_step, trigger),
        ]
