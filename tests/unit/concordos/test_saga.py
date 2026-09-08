# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the WorkflowSaga (ADR-069 §4).

These tests follow the project's behaviour-test convention:
they build a real ``World`` via the SUT builders in
``kntgraph.testing`` and call the system against it. No
mocks on ``ReactiveDispatcher``. They run with
``KNT_REDIS_FAKE=1``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from uuid import uuid4

from kntgraph.concordos.saga import (
    SagaConfig,
    SagaProgressComponent,
    SagaStepConfig,
    SagaSystem,
    SagaTimeoutSystem,
)
from kntgraph.concordos.specs import StepFailed, StepTimedOut
from kntgraph.core.world.components import ToolCallCompletion, ToolCallRequest
from kntgraph.testing import AgentViewBuilder, WorldBuilder, run_system

FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _request(req_eid: str, tool_name: str) -> ToolCallRequest:
    """Build a ``ToolCallRequest`` for the given tool."""
    return ToolCallRequest(
        request_event_id=req_eid,
        tool_name=tool_name,
        agent_id="agent-1",
        params={},
        requested_at=FIXED_NOW,
    )


def _saga_config() -> SagaConfig:
    """The NF-e emission saga from ADR-069 §4.8 (simplified)."""
    return SagaConfig(
        name="nfe_emission",
        saga_timeout_ms=300_000,
        fail_when=StepFailed("emit_nfe").and_(StepFailed("emit_nfce")),
        steps=(
            SagaStepConfig(
                name="validate_fiscal",
                tool_name="sefaz_validator",
                timeout_ms=10_000,
            ),
            SagaStepConfig(
                name="emit_nfe",
                tool_name="nfe_emitter",
                compensate_tool="nfe_canceller",
                compensate_when=StepTimedOut("emit_nfe").not_(),
                enrich_from=("cfop", "tax_amount"),
                timeout_ms=30_000,
            ),
            SagaStepConfig(
                name="register_receivable",
                tool_name="erp_receivable_tool",
                compensate_tool="erp_reversal_tool",
                timeout_ms=15_000,
            ),
        ),
    )


def _progress(
    *,
    current_step: str,
    direction: str = "forward",
    step_states: dict[str, str] | None = None,
    step_results: dict[str, object] | None = None,
    compensate_stack: tuple[str, ...] = (),
    started_at: datetime = FIXED_NOW,
    step_order: tuple[str, ...] = (
        "validate_fiscal",
        "emit_nfe",
        "register_receivable",
    ),
) -> SagaProgressComponent:
    """Build a ``SagaProgressComponent`` with the given state."""
    return SagaProgressComponent(
        saga_id="saga-001",
        saga_name="nfe_emission",
        current_step=current_step,
        direction=direction,
        step_order=step_order,
        step_states=MappingProxyType(step_states or {}),
        step_results=MappingProxyType(step_results or {}),
        compensate_stack=compensate_stack,
        started_at=started_at,
    )


def test_saga_dispatches_first_step_on_start() -> None:
    """On ``saga.nfe_emission.started``, the first non-skipped
    step is dispatched."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="validate_fiscal",
                step_states={"validate_fiscal": "pending"},
            )
        )
        .with_trigger("saga.nfe_emission.started", data={"saga_id": "saga-001"})
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "tool.sefaz_validator.requested" for e in out)


def test_saga_skips_nfe_when_not_required() -> None:
    """When validate_fiscal completed with nfe_required=False, the
    emit_nfe step is skipped (its skip_when is satisfied)."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"validate_fiscal": "completed", "emit_nfe": "in_flight"},
                step_results={"validate_fiscal": {"nfe_required": False}},
            )
        )
        .with_tool_request(_request(req_eid, "sefaz_validator"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="completed",
                result={"nfe_required": False},
            ),
        )
        .with_trigger("tool.sefaz_validator.completed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert not any(e.event_type == "tool.nfe_emitter.requested" for e in out)


def test_saga_compensates_on_timeout_except_timed_out_steps() -> None:
    """When emit_nfe timed out, the nfe_canceller is NOT dispatched
    (compensate_when blocks it)."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"validate_fiscal": "completed", "emit_nfe": "timed_out"},
                step_results={
                    "validate_fiscal": {"nfe_required": True},
                    "emit_nfe": {},
                },
                compensate_stack=("validate_fiscal", "emit_nfe"),
            )
        )
        .with_tool_request(_request(req_eid, "nfe_emitter"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="timed_out",
                error="ttl_expired",
            ),
        )
        .with_trigger("tool.nfe_emitter.timed_out")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert not any(e.event_type == "tool.nfe_canceller.requested" for e in out)


def test_saga_timeout_system_emits_timed_out() -> None:
    """A saga started 6 minutes ago (timeout=5min) emits
    ``saga.nfe_emission.timed_out``."""
    past = FIXED_NOW - timedelta(minutes=6)
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"emit_nfe": "in_flight"},
                compensate_stack=("validate_fiscal",),
                started_at=past,
            )
        )
        .with_trigger("saga.nfe_emission.started")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    system = SagaTimeoutSystem({"nfe_emission": _saga_config()}, now=lambda: FIXED_NOW)
    out = run_system(system, world)
    assert any(e.event_type == "saga.nfe_emission.timed_out" for e in out)


def test_saga_dlq_event_emitted_on_compensation_failure() -> None:
    """A saga in ``compensating`` whose last event was a failed
    compensation tool emits ``saga.nfe_emission.dlq``."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                direction="compensating",
                step_states={"emit_nfe": "compensation_failed"},
                compensate_stack=("emit_nfe",),
            )
        )
        .with_tool_request(_request(req_eid, "nfe_canceller"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="failed",
                error="se_faz_offline",
            ),
        )
        .with_trigger("tool.nfe_canceller.failed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "saga.nfe_emission.dlq" for e in out)


def test_saga_advances_to_next_step_on_completion() -> None:
    """A completed step advances the saga to the next non-skipped
    step (dispatching its tool)."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="validate_fiscal",
                step_states={"validate_fiscal": "in_flight"},
            )
        )
        .with_tool_request(_request(req_eid, "sefaz_validator"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="completed",
                result={"nfe_required": True},
            ),
        )
        .with_trigger("tool.sefaz_validator.completed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "tool.nfe_emitter.requested" for e in out)


def test_saga_completes_when_last_step_done() -> None:
    """When the last step completes, the saga emits
    ``saga.nfe_emission.completed``."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="register_receivable",
                step_states={
                    "validate_fiscal": "completed",
                    "emit_nfe": "completed",
                    "register_receivable": "in_flight",
                },
            )
        )
        .with_tool_request(_request(req_eid, "erp_receivable_tool"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="completed",
                result={"receivable_id": "r-1"},
            ),
        )
        .with_trigger("tool.erp_receivable_tool.completed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "saga.nfe_emission.completed" for e in out)


def test_saga_compensates_when_compensate_when_satisfied() -> None:
    """A step whose ``compensate_when`` is satisfied (not a
    timed-out step) IS compensated on rollback."""
    # A config that fails on the first step failure (default).
    fail_first = SagaConfig(
        name="nfe_emission",
        steps=_saga_config().steps,
    )
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"validate_fiscal": "completed", "emit_nfe": "failed"},
                step_results={
                    "validate_fiscal": {"nfe_required": True},
                    "emit_nfe": {},
                },
                compensate_stack=("validate_fiscal", "emit_nfe"),
            )
        )
        .with_tool_request(_request(req_eid, "nfe_emitter"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="failed",
                error="se_faz_offline",
            ),
        )
        .with_trigger("tool.nfe_emitter.failed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(fail_first, now=lambda: FIXED_NOW), world)
    # emit_nfe failed (not timed out) → nfe_canceller IS dispatched.
    assert any(e.event_type == "tool.nfe_canceller.requested" for e in out)


def test_saga_timeout_system_skips_non_forward() -> None:
    """A saga not in ``forward`` direction is not timed out."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                direction="compensating",
                step_states={"emit_nfe": "in_flight"},
            )
        )
        .with_trigger("saga.nfe_emission.started")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    system = SagaTimeoutSystem({"nfe_emission": _saga_config()}, now=lambda: FIXED_NOW)
    out = run_system(system, world)
    assert out == []


def test_saga_timeout_system_skips_unknown_saga() -> None:
    """A saga whose name is not in the config map is not timed
    out."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"emit_nfe": "in_flight"},
            )
        )
        .with_trigger("saga.nfe_emission.started")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    system = SagaTimeoutSystem({}, now=lambda: FIXED_NOW)
    out = run_system(system, world)
    assert out == []


def test_saga_timeout_system_skips_not_yet_expired() -> None:
    """A saga that has not yet exceeded its timeout emits
    nothing."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"emit_nfe": "in_flight"},
                started_at=FIXED_NOW - timedelta(minutes=1),
            )
        )
        .with_trigger("saga.nfe_emission.started")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    system = SagaTimeoutSystem({"nfe_emission": _saga_config()}, now=lambda: FIXED_NOW)
    out = run_system(system, world)
    assert out == []


def test_saga_step_config_rejects_empty_tool_name() -> None:
    """An empty ``tool_name`` (a typo for the ``None`` human-step
    signal) is rejected at construction."""
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        SagaStepConfig(name="bad", tool_name="")


def test_saga_proceed_when_failure_treated_as_failure() -> None:
    """A step whose ``proceed_when`` is not satisfied after a
    successful tool call is treated as a failure (and the saga
    compensates)."""
    from kntgraph.concordos.specs import StepResultEquals

    proceed_config = SagaConfig(
        name="nfe_emission",
        steps=(
            SagaStepConfig(
                name="validate_fiscal",
                tool_name="sefaz_validator",
                proceed_when=StepResultEquals("validate_fiscal", "nfe_required", True),
            ),
        ),
    )
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="validate_fiscal",
                step_states={"validate_fiscal": "in_flight"},
            )
        )
        .with_tool_request(_request(req_eid, "sefaz_validator"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="completed",
                result={"nfe_required": False},
            ),
        )
        .with_trigger("tool.sefaz_validator.completed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(proceed_config, now=lambda: FIXED_NOW), world)
    # proceed_when not met → treated as failure → saga compensates.
    assert any(e.event_type == "saga.nfe_emission.compensating" for e in out)


def test_saga_start_completes_when_all_steps_skipped() -> None:
    """When every step is skipped, the saga completes immediately
    on start."""
    from kntgraph.concordos.specs import StepCompleted

    skip_all = SagaConfig(
        name="nfe_emission",
        steps=(
            SagaStepConfig(
                name="validate_fiscal",
                tool_name="sefaz_validator",
                skip_when=StepCompleted("validate_fiscal"),
            ),
        ),
    )
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="validate_fiscal",
                step_states={"validate_fiscal": "completed"},
            )
        )
        .with_trigger("saga.nfe_emission.started", data={"saga_id": "saga-001"})
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(skip_all, now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "saga.nfe_emission.completed" for e in out)


def test_saga_continues_after_non_fatal_failure() -> None:
    """When ``fail_when`` is not satisfied, a failed step does not
    fail the saga; it continues to the next step."""
    # fail_when requires BOTH emit_nfe and emit_nfce to fail; a
    # single failure continues.
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"validate_fiscal": "completed", "emit_nfe": "failed"},
                step_results={
                    "validate_fiscal": {"nfe_required": True},
                    "emit_nfe": {},
                },
                compensate_stack=("validate_fiscal",),
            )
        )
        .with_tool_request(_request(req_eid, "nfe_emitter"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="failed",
                error="se_faz_offline",
            ),
        )
        .with_trigger("tool.nfe_emitter.failed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    # Continues to the next step (register_receivable) instead of
    # compensating.
    assert any(e.event_type == "tool.erp_receivable_tool.requested" for e in out)
    assert not any(e.event_type == "saga.nfe_emission.compensating" for e in out)


def test_saga_ignores_tool_event_without_matching_step() -> None:
    """A tool completion whose current step is not in the config
    (or has no completion folded yet) emits nothing."""
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="unknown_step",
                step_states={"unknown_step": "in_flight"},
            )
        )
        .with_tool_request(_request(req_eid, "some_tool"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="completed",
                result={},
            ),
        )
        .with_trigger("tool.some_tool.completed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert out == []


def test_saga_human_step_emits_awaiting_approval() -> None:
    """A step with ``tool_name=None`` (human step) emits
    ``saga.<name>.<step>.awaiting_approval`` instead of a tool
    request."""
    human_config = SagaConfig(
        name="nfe_emission",
        steps=(SagaStepConfig(name="approve", tool_name=None),),
    )
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="approve",
                step_states={"approve": "pending"},
                step_order=("approve",),
            )
        )
        .with_trigger("saga.nfe_emission.started", data={"saga_id": "saga-001"})
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(human_config, now=lambda: FIXED_NOW), world)
    assert any(
        e.event_type == "saga.nfe_emission.approve.awaiting_approval" for e in out
    )


def test_saga_compensates_step_without_compensate_tool() -> None:
    """A step on the compensate_stack with no ``compensate_tool``
    is skipped during compensation (no compensation event)."""
    no_comp_config = SagaConfig(
        name="nfe_emission",
        steps=(SagaStepConfig(name="validate_fiscal", tool_name="sefaz_validator"),),
    )
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="validate_fiscal",
                step_states={"validate_fiscal": "failed"},
                compensate_stack=("validate_fiscal",),
            )
        )
        .with_tool_request(_request(req_eid, "sefaz_validator"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="failed",
                error="err",
            ),
        )
        .with_trigger("tool.sefaz_validator.failed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(no_comp_config, now=lambda: FIXED_NOW), world)
    # validate_fiscal has no compensate_tool → only the
    # compensating marker is emitted, no tool compensation.
    assert any(e.event_type == "saga.nfe_emission.compensating" for e in out)
    assert not any(e.event_type.startswith("tool.") for e in out)


def test_saga_events_for_agent_guards_missing_component() -> None:
    """The defensive ``saga is None`` guard returns ``[]``."""
    view = AgentViewBuilder("agent-1").with_trigger("saga.nfe_emission.started").build()
    system = SagaSystem(_saga_config(), now=lambda: FIXED_NOW)
    world = WorldBuilder().with_agent(view).build()
    assert system._events_for_agent(view, world) == []


def test_saga_events_for_agent_guards_missing_trigger() -> None:
    """The defensive ``trigger_type is None`` guard returns
    ``[]``."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(_progress(current_step="validate_fiscal"))
        .build()
    )
    system = SagaSystem(_saga_config(), now=lambda: FIXED_NOW)
    world = WorldBuilder().with_agent(view).build()
    assert system._events_for_agent(view, world) == []


def test_saga_ignores_non_tool_trigger() -> None:
    """A trigger that is not a tool completion / failure / timeout
    (and not a saga event) emits nothing."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(_progress(current_step="validate_fiscal"))
        .with_trigger("some.other.event")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert out == []


def test_saga_compensates_on_saga_timeout() -> None:
    """A ``saga.<name>.timed_out`` trigger while forward begins
    compensation."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"emit_nfe": "in_flight"},
                compensate_stack=("validate_fiscal",),
            )
        )
        .with_trigger("saga.nfe_emission.timed_out")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "saga.nfe_emission.compensating" for e in out)


def test_saga_ignores_saga_timeout_when_not_forward() -> None:
    """A ``saga.<name>.timed_out`` trigger while NOT forward emits
    nothing."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                direction="compensating",
                step_states={"emit_nfe": "in_flight"},
            )
        )
        .with_trigger("saga.nfe_emission.timed_out")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert out == []


def test_saga_dlq_on_compensation_failed_trigger() -> None:
    """A ``saga.<name>.compensation_failed`` trigger emits the DLQ
    event."""
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                direction="compensating",
                step_states={"emit_nfe": "compensation_failed"},
            )
        )
        .with_trigger("saga.nfe_emission.compensation_failed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "saga.nfe_emission.dlq" for e in out)


def test_saga_compensates_step_without_compensate_when() -> None:
    """A step with ``compensate_when=None`` (always compensate) is
    compensated on rollback."""
    always_comp = SagaConfig(
        name="nfe_emission",
        steps=(
            SagaStepConfig(
                name="emit_nfe",
                tool_name="nfe_emitter",
                compensate_tool="nfe_canceller",
            ),
        ),
    )
    req_eid = str(uuid4())
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"emit_nfe": "failed"},
                compensate_stack=("emit_nfe",),
            )
        )
        .with_tool_request(_request(req_eid, "nfe_emitter"))
        .with_tool_completion(
            req_eid,
            ToolCallCompletion(
                request_event_id=req_eid,
                status="failed",
                error="err",
            ),
        )
        .with_trigger("tool.nfe_emitter.failed")
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(always_comp, now=lambda: FIXED_NOW), world)
    assert any(e.event_type == "tool.nfe_canceller.requested" for e in out)


def test_saga_step_result_payload_non_mapping() -> None:
    """``_step_result_payload`` returns ``{}`` when the step result
    is not a mapping."""
    system = SagaSystem(_saga_config(), now=lambda: FIXED_NOW)
    saga = _progress(
        current_step="emit_nfe",
        step_results={"emit_nfe": "not-a-mapping"},
    )
    assert system._step_result_payload(saga, "emit_nfe") == {}


def test_saga_dispatch_enrich_from_non_mapping_previous() -> None:
    """``_dispatch_step`` skips enrichment when the previous
    step_results is not a mapping."""
    enrich_config = SagaConfig(
        name="nfe_emission",
        steps=(
            SagaStepConfig(
                name="emit_nfe",
                tool_name="nfe_emitter",
                enrich_from=("cfop",),
            ),
        ),
    )
    view = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step="emit_nfe",
                step_states={"emit_nfe": "in_flight"},
            )
        )
        .with_trigger(
            "saga.nfe_emission.started",
            data={"saga_id": "saga-001", "step_results": "not-a-mapping"},
        )
        .build()
    )
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(enrich_config, now=lambda: FIXED_NOW), world)
    requested = next(e for e in out if e.event_type == "tool.nfe_emitter.requested")
    assert "cfop" not in requested.data


def test_saga_next_non_skipped_step_unknown_current() -> None:
    """``_next_non_skipped_step`` returns ``None`` when the current
    step is not in the declared order."""
    from kntgraph.concordos.base import StepContext

    system = SagaSystem(_saga_config(), now=lambda: FIXED_NOW)
    unknown = SagaStepConfig(name="ghost", tool_name="ghost_tool")
    ctx = StepContext(
        step_results=MappingProxyType({}),
        step_states=MappingProxyType({}),
        domain=None,
        continuity=None,
        profile=None,
        world=WorldBuilder().build(),
        agent_id="agent-1",
        now=FIXED_NOW,
    )
    assert system._next_non_skipped_step(unknown, ctx) is None
