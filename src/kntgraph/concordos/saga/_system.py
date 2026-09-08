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

The system is pure: same ``World`` ⇒ same ``list[Event]``.
``now`` is injected (defaults to the framework clock) so a
replayed log re-evaluates guards / timeouts with the same
timestamp.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING
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
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.clock import Clock
    from kntgraph.core.event.event import Event
    from kntgraph.core.world.view import AgentView

__all__ = ["SagaSystem"]


class SagaSystem:
    """
    C-02: WorkflowSaga — WorldSystem (post-ADR-018).

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
        trigger_type = view.domain_phase
        if trigger_type is None:
            return []
        trigger = ViewTrigger(
            agent_id=view.agent_id,
            event_type=trigger_type,
            event_id=UUID(str(view.last_event_id))
            if view.last_event_id is not None
            else None,
            data=view.components.get(trigger_type, {}),
            correlation=correlation_middleware.current(),
        )

        # Saga start
        if trigger.event_type == f"saga.{self._cfg.name}.started":
            return self._start(view, trigger, saga)

        # Saga-level timeout (from SagaTimeoutSystem)
        if trigger.event_type == f"saga.{self._cfg.name}.timed_out":
            if saga.direction == "forward":
                return self._begin_compensation(
                    world, view, saga, trigger, reason="saga_timeout"
                )
            return []

        # Compensation failure (§4.5.1): escalate to DLQ.
        if trigger.event_type == f"saga.{self._cfg.name}.compensation_failed":
            return [self._dlq_event(saga, trigger)]

        # A compensation tool that itself fails while the saga is
        # compensating (§4.5.1): emit ``compensation_failed`` then
        # ``dlq`` so the operator can intervene.
        if saga.direction == "compensating" and self._is_compensation_failure(
            trigger, saga
        ):
            return [
                self._emit(
                    trigger,
                    event_type=f"saga.{self._cfg.name}.compensation_failed",
                    data={"saga_id": saga.saga_id, "stuck_step": saga.current_step},
                ),
                self._dlq_event(saga, trigger),
            ]

        # Tool completion / failure / timeout
        if not (
            trigger.event_type.startswith("tool.")
            and trigger.event_type.endswith((".completed", ".failed", ".timed_out"))
        ):
            return []

        step_config = self._match_step(view, trigger, saga)
        if step_config is None:
            return []

        return self._handle_completion(view, world, saga, step_config, trigger)

    # ------------------------------------------------------------------
    # _match_step — the join key for saga ↔ tool completion.
    # ------------------------------------------------------------------
    def _match_step(
        self,
        view: "AgentView",
        trigger: "ViewTrigger",
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
        """True when the trigger is a ``tool.<name>.failed`` event
        for a compensation tool of a step on the compensate_stack
        (i.e. a compensation attempt that itself failed)."""
        if not trigger.event_type.endswith(".failed"):
            return False
        for step_name in saga.compensate_stack:
            step_cfg = self._step_map.get(step_name)
            if step_cfg is None or step_cfg.compensate_tool is None:
                continue
            if trigger.event_type == f"tool.{step_cfg.compensate_tool}.failed":
                return True
        return False

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
    # _start / _handle_completion / _advance / _handle_failure
    # ------------------------------------------------------------------
    def _start(
        self,
        view: "AgentView",
        trigger: "ViewTrigger",
        saga: SagaProgressComponent,
    ) -> list["Event"]:
        """Dispatch the first non-skipped step."""
        step_config = self._first_non_skipped_step(saga, trigger)
        if step_config is None:
            # All steps skipped: saga completes immediately
            return [self._saga_completed(trigger, saga)]
        return [
            self._record_start(trigger, saga, step_config),
            self._dispatch_step(step_config, trigger),
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
            world=world,
            agent_id=view.agent_id,
            now=self._now(),
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
                saga, step_config, trigger, ctx, new_states, new_results
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
        saga: SagaProgressComponent,
        current_step: "SagaStepConfig",
        trigger: "ViewTrigger",
        ctx: StepContext,
        new_states: dict,
        new_results: dict,
    ) -> list["Event"]:
        """Move to the next non-skipped step or complete the saga."""
        next_step = self._next_non_skipped_step(current_step, ctx)
        record = self._record_step_completed(
            saga, current_step, trigger, new_states, new_results
        )
        if next_step is None:
            return [record, self._saga_completed(trigger, saga)]
        return [record, self._dispatch_step(next_step, trigger)]

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
        fail_spec = self._cfg.fail_when
        should_fail = (
            fail_spec.is_satisfied_by(ctx)
            if fail_spec is not None
            else True  # default: fail on first failure
        )
        record = self._record_step_failed(
            saga, step_config, trigger, new_states, new_results
        )
        if should_fail:
            return [record] + self._begin_compensation(
                world, view, saga, trigger, reason="step_failure"
            )
        # continue to next step despite this step's failure
        next_step = self._next_non_skipped_step(step_config, ctx)
        if next_step is None:
            return [record, self._saga_completed(trigger, saga)]
        return [record, self._dispatch_step(next_step, trigger)]

    # ------------------------------------------------------------------
    # _begin_compensation — LIFO with per-step compensate_when
    # ------------------------------------------------------------------
    def _begin_compensation(
        self,
        world: "World",
        view: "AgentView",
        saga: SagaProgressComponent,
        trigger: "ViewTrigger",
        reason: str,
    ) -> list["Event"]:
        """
        Emit compensation events in LIFO order.

        For each step on the compensate_stack (the steps that
        already produced an external effect and need to be
        rolled back), we check the step's ``compensate_when``
        Specification. A step whose compensation would be a
        no-op (e.g. a timed-out NF-e emission that never
        landed) is skipped — see §4.8 example.

        If a compensation tool itself fails, the saga emits
        ``saga.<name>.compensation_failed`` and the system
        routes the agent to the DLQ on the next tick (§4.5.1).
        """
        out: list[Event] = [
            self._emit(
                trigger,
                event_type=f"saga.{self._cfg.name}.compensating",
                data={"reason": reason, "saga_id": saga.saga_id},
            )
        ]
        ctx = StepContext(
            step_results=saga.step_results,
            step_states=saga.step_states,
            domain=None,
            continuity=None,
            profile=None,
            world=world,
            agent_id=view.agent_id,
            now=self._now(),
        )
        for step_name in reversed(saga.compensate_stack):
            step_cfg = self._step_map.get(step_name)
            if step_cfg is None or step_cfg.compensate_tool is None:
                continue
            if (
                step_cfg.compensate_when is not None
                and not step_cfg.compensate_when.is_satisfied_by(ctx)
            ):
                continue
            out.append(
                self._emit(
                    trigger,
                    event_type=f"tool.{step_cfg.compensate_tool}.requested",
                    data={
                        "saga_id": saga.saga_id,
                        "compensating_step": step_name,
                        **self._step_result_payload(saga, step_name),
                    },
                )
            )
        return out

    def _step_result_payload(
        self,
        saga: SagaProgressComponent,
        step_name: str,
    ) -> dict[str, "JsonValue"]:
        """Return the step's result payload (a ``dict[str,
        JsonValue]``) for enrichment, or ``{}`` when the result is
        not a mapping."""
        result = saga.step_results.get(step_name)
        if isinstance(result, Mapping):
            return dict(result)
        return {}

    # ------------------------------------------------------------------
    # _dispatch_step — emit tool.<name>.requested
    # ------------------------------------------------------------------
    def _dispatch_step(
        self,
        step_config: "SagaStepConfig",
        trigger: "ViewTrigger",
    ) -> "Event":
        """
        Emit ``tool.<name>.requested`` for the step.

        Human steps (``tool_name is None``) are NOT dispatched
        via ``tool.<name>.requested``. Instead they emit
        ``saga.<step_name>.awaiting_approval`` and the saga
        blocks until a corresponding ``saga.<step_name>.approved``
        / ``saga.<step_name>.rejected`` event arrives (see §9.2
        item 3 for the open question on human-step timeouts).
        """
        if step_config.tool_name is None:
            return self._emit(
                trigger,
                event_type=(
                    f"saga.{self._cfg.name}.{step_config.name}.awaiting_approval"
                ),
                data={"step_name": step_config.name},
            )
        params: dict[str, "JsonValue"] = {
            "saga_id": trigger.data.get("saga_id", ""),
        }
        # Enrich from previous step results (read via the
        # trigger's data envelope — the saga-system projection
        # attaches the latest step_results to the saga-component
        # clone carried on the dispatch event; see
        # _record_step_completed).
        previous = trigger.data.get("step_results", {})
        if isinstance(previous, Mapping):
            for field in step_config.enrich_from:
                for prev_result in previous.values():
                    if isinstance(prev_result, Mapping) and field in prev_result:
                        params.setdefault(field, prev_result[field])
        return self._emit(
            trigger,
            event_type=f"tool.{step_config.tool_name}.requested",
            data=params,
        )

    # ------------------------------------------------------------------
    # _record_* helpers, _saga_completed, _first/_next_non_skipped_step
    # ------------------------------------------------------------------
    def _record_start(
        self,
        trigger: "ViewTrigger",
        saga: SagaProgressComponent,
        step_config: "SagaStepConfig",
    ) -> "Event":
        """Record the saga start (the first step is now in
        flight)."""
        return self._emit(
            trigger,
            event_type=f"saga.{self._cfg.name}.step_started",
            data={"step_name": step_config.name, "saga_id": saga.saga_id},
        )

    def _record_step_completed(
        self,
        saga: SagaProgressComponent,
        step_config: "SagaStepConfig",
        trigger: "ViewTrigger",
        new_states: dict,
        new_results: dict,
    ) -> "Event":
        """Record a step completion, carrying the updated
        step_states / step_results so the next dispatch can
        enrich from them."""
        return self._emit(
            trigger,
            event_type=f"saga.{self._cfg.name}.step_completed",
            data={
                "step_name": step_config.name,
                "saga_id": saga.saga_id,
                "step_states": dict(new_states),
                "step_results": dict(new_results),
            },
        )

    def _record_step_failed(
        self,
        saga: SagaProgressComponent,
        step_config: "SagaStepConfig",
        trigger: "ViewTrigger",
        new_states: dict,
        new_results: dict,
    ) -> "Event":
        """Record a step failure, carrying the updated
        step_states / step_results."""
        return self._emit(
            trigger,
            event_type=f"saga.{self._cfg.name}.step_failed",
            data={
                "step_name": step_config.name,
                "saga_id": saga.saga_id,
                "step_states": dict(new_states),
                "step_results": dict(new_results),
            },
        )

    def _saga_completed(
        self,
        trigger: "ViewTrigger",
        saga: SagaProgressComponent,
    ) -> "Event":
        """Emit the saga-completed event."""
        return self._emit(
            trigger,
            event_type=f"saga.{self._cfg.name}.completed",
            data={"saga_id": saga.saga_id},
        )

    def _first_non_skipped_step(
        self,
        saga: SagaProgressComponent,
        trigger: "ViewTrigger",
    ) -> "SagaStepConfig | None":
        """Return the first step in declared order that is not
        skipped (per its ``skip_when`` Specification)."""
        ctx = StepContext(
            step_results=saga.step_results,
            step_states=saga.step_states,
            domain=None,
            continuity=None,
            profile=None,
            world=World.empty(),
            agent_id=trigger.agent_id,
            now=self._now(),
        )
        for step_name in saga.step_order:
            step_cfg = self._step_map.get(step_name)
            if step_cfg is None:
                continue
            if step_cfg.skip_when is not None and step_cfg.skip_when.is_satisfied_by(
                ctx
            ):
                continue
            return step_cfg
        return None

    def _next_non_skipped_step(
        self,
        current_step: "SagaStepConfig",
        ctx: StepContext,
    ) -> "SagaStepConfig | None":
        """Return the next step after ``current_step`` in declared
        order that is not skipped."""
        order = self._cfg.steps
        try:
            idx = order.index(current_step)
        except ValueError:
            return None
        for step_cfg in order[idx + 1 :]:
            if step_cfg.skip_when is not None and step_cfg.skip_when.is_satisfied_by(
                ctx
            ):
                continue
            return step_cfg
        return None

    # ------------------------------------------------------------------
    # 4.5.1 DLQ on compensation failure
    # ------------------------------------------------------------------
    def _dlq_event(
        self,
        saga: SagaProgressComponent,
        trigger: "ViewTrigger",
    ) -> "Event":
        """
        Build the DLQ-emission domain event for a saga whose
        compensation could not be completed.

        The actual DLQ insertion is performed by an adapter
        system that reads this event and appends to
        ``knt:dlq:saga:<name>``; the saga system only emits the
        typed event so the DLQ adapter stays out of the saga's
        dependency graph.
        """
        return self._emit(
            trigger,
            event_type=f"saga.{self._cfg.name}.dlq",
            data={
                "saga_id": saga.saga_id,
                "stuck_step": saga.current_step,
                "step_states": dict(saga.step_states),
            },
        )

    def _emit(
        self,
        trigger: "ViewTrigger",
        *,
        event_type: str,
        data: dict[str, "JsonValue"],
    ) -> "Event":
        from kntgraph.core.event.event import Event

        return Event.create(
            agent_id=trigger.agent_id,
            event_type=event_type,
            event_class="domain",
            data=data,
            causation_id=trigger.event_id,
            correlation=trigger.correlation,
        )
