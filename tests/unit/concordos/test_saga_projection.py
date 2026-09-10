# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour tests for the saga progress reconciliation (ADR-069 §9.2 item 6).

The ``SagaProjection`` re-derives ``SagaProgressComponent`` from a
batch of saga events, so a re-fold of the EventLog reconstructs the
same progress without an in-memory cache. These tests build a real
``World`` via the SUT builders and run the projection against it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kntgraph.concordos.saga import (
    SagaConfig,
    SagaProgressComponent,
    SagaProjection,
    SagaStepConfig,
)
from kntgraph.core.event import Event
from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.testing import AgentViewBuilder, WorldBuilder

FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _saga_config() -> SagaConfig:
    """The NF-e emission saga from ADR-069 §4.8 (simplified)."""
    return SagaConfig(
        name="nfe_emission",
        saga_timeout_ms=300_000,
        steps=(
            SagaStepConfig(
                name="validate_fiscal",
                tool_name="sefaz_validator",
            ),
            SagaStepConfig(
                name="emit_nfe",
                tool_name="nfe_emitter",
                compensate_tool="nfe_canceller",
            ),
            SagaStepConfig(
                name="register_receivable",
                tool_name="erp_receivable_tool",
                compensate_tool="erp_reversal_tool",
            ),
        ),
    )


def _event(
    agent_id: str,
    event_type: str,
    data: dict,
) -> Event:
    return Event.create(
        agent_id=agent_id,
        event_type=event_type,
        event_class="domain",
        data=data,
        correlation=CorrelationContext.new(),
    )


def _run_projection(
    config: SagaConfig,
    events: list[Event],
    *,
    base: SagaProgressComponent | None = None,
) -> SagaProgressComponent | None:
    """Build a World with one agent and run the projection."""
    builder = AgentViewBuilder("agent-1")
    if base is not None:
        builder = builder.with_component(base)
    view = builder.build()
    world = WorldBuilder().with_agent(view).build()
    projection = SagaProjection(config)
    new_world = projection(world, events)
    return new_world.get_agent("agent-1").get_component(SagaProgressComponent)


def test_projection_materialises_on_started() -> None:
    """``saga.<name>.started`` materialises the component in
    ``forward`` with the saga_id."""
    events = [
        _event("agent-1", "saga.nfe_emission.started", {"saga_id": "saga-001"}),
    ]
    saga = _run_projection(_saga_config(), events)
    assert saga is not None
    assert saga.saga_id == "saga-001"
    assert saga.saga_name == "nfe_emission"
    assert saga.direction == "forward"
    assert saga.step_order == ("validate_fiscal", "emit_nfe", "register_receivable")


def test_projection_tracks_step_started() -> None:
    """``step_started`` sets ``current_step`` and marks it in flight."""
    events = [
        _event("agent-1", "saga.nfe_emission.started", {"saga_id": "saga-001"}),
        _event(
            "agent-1",
            "saga.nfe_emission.step_started",
            {"step_name": "validate_fiscal", "saga_id": "saga-001"},
        ),
    ]
    saga = _run_projection(_saga_config(), events)
    assert saga is not None
    assert saga.current_step == "validate_fiscal"
    assert saga.step_states["validate_fiscal"] == "in_flight"


def test_projection_carries_step_snapshot() -> None:
    """``step_completed`` carries the updated step_states /
    step_results onto the component."""
    events = [
        _event("agent-1", "saga.nfe_emission.started", {"saga_id": "saga-001"}),
        _event(
            "agent-1",
            "saga.nfe_emission.step_completed",
            {
                "step_name": "validate_fiscal",
                "saga_id": "saga-001",
                "step_states": {"validate_fiscal": "completed"},
                "step_results": {"validate_fiscal": {"nfe_required": True}},
            },
        ),
    ]
    saga = _run_projection(_saga_config(), events)
    assert saga is not None
    assert saga.step_states["validate_fiscal"] == "completed"
    assert saga.step_results["validate_fiscal"] == {"nfe_required": True}


def test_projection_compensating_builds_stack() -> None:
    """``compensating`` sets direction and builds the
    compensate_stack from the completed steps that carry a
    compensate_tool, in LIFO order."""
    events = [
        _event("agent-1", "saga.nfe_emission.started", {"saga_id": "saga-001"}),
        _event(
            "agent-1",
            "saga.nfe_emission.step_completed",
            {
                "step_name": "validate_fiscal",
                "saga_id": "saga-001",
                "step_states": {"validate_fiscal": "completed"},
                "step_results": {"validate_fiscal": {}},
            },
        ),
        _event(
            "agent-1",
            "saga.nfe_emission.step_completed",
            {
                "step_name": "emit_nfe",
                "saga_id": "saga-001",
                "step_states": {
                    "validate_fiscal": "completed",
                    "emit_nfe": "completed",
                },
                "step_results": {"validate_fiscal": {}, "emit_nfe": {}},
            },
        ),
        _event(
            "agent-1",
            "saga.nfe_emission.compensating",
            {"reason": "step_failure", "saga_id": "saga-001"},
        ),
    ]
    saga = _run_projection(_saga_config(), events)
    assert saga is not None
    assert saga.direction == "compensating"
    # Only emit_nfe (not validate_fiscal) carries a compensate_tool.
    assert saga.compensate_stack == ("emit_nfe",)


def test_projection_completed_sets_done() -> None:
    """``saga.<name>.completed`` sets direction to ``done``."""
    events = [
        _event("agent-1", "saga.nfe_emission.started", {"saga_id": "saga-001"}),
        _event("agent-1", "saga.nfe_emission.completed", {"saga_id": "saga-001"}),
    ]
    saga = _run_projection(_saga_config(), events)
    assert saga is not None
    assert saga.direction == "done"


def test_projection_dlq_sets_compensation_failed() -> None:
    """``saga.<name>.dlq`` sets direction to ``compensation_failed``
    and records the stuck step."""
    events = [
        _event("agent-1", "saga.nfe_emission.started", {"saga_id": "saga-001"}),
        _event(
            "agent-1",
            "saga.nfe_emission.dlq",
            {"saga_id": "saga-001", "stuck_step": "emit_nfe"},
        ),
    ]
    saga = _run_projection(_saga_config(), events)
    assert saga is not None
    assert saga.direction == "compensation_failed"
    assert saga.current_step == "emit_nfe"


def test_projection_returns_none_without_saga_events() -> None:
    """An agent with no saga event and no base component produces
    no saga component (no allocation)."""
    events = [
        _event("agent-1", "user.intent", {"message": "hi"}),
    ]
    saga = _run_projection(_saga_config(), events)
    assert saga is None


def test_projection_preserves_base_without_saga_events() -> None:
    """An agent with a base saga component but no saga event in the
    batch keeps the base component (the fold threads it through)."""
    base = SagaProgressComponent(
        saga_id="saga-001",
        saga_name="nfe_emission",
        current_step="validate_fiscal",
        direction="forward",
        step_order=("validate_fiscal", "emit_nfe", "register_receivable"),
        step_states=__import__("types").MappingProxyType(
            {"validate_fiscal": "in_flight"}
        ),
        step_results=__import__("types").MappingProxyType({}),
        compensate_stack=(),
        started_at=FIXED_NOW,
    )
    events = [
        _event("agent-1", "user.intent", {"message": "hi"}),
    ]
    saga = _run_projection(_saga_config(), events, base=base)
    assert saga is not None
    assert saga.current_step == "validate_fiscal"
