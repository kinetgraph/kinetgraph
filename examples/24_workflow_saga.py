# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 24: WorkflowSaga — orchestrate a sequence of tool calls (ADR-069 §4).

This example demonstrates the **WorkflowSaga Concordo** (C-02):
a reactive system that orchestrates a sequence of tool calls
with context enrichment, skip conditions, failure policies, and
compensation. It is built on top of the framework's tool-call
primitives (ADR-034) and does NOT reinvent the tool lifecycle.

The example models an **NF-e emission saga**:

```
saga.nfe_emission.started
  → tool.sefaz_validator.requested      (validate_fiscal)
  → tool.sefaz_validator.completed      {nfe_required: true}
  → tool.nfe_emitter.requested          (emit_nfe, enriched from validate)
  → tool.nfe_emitter.completed          {nfe_key: "..."}
  → tool.erp_receivable_tool.requested  (register_receivable)
  → tool.erp_receivable_tool.completed
  → saga.nfe_emission.completed
```

## What the example shows

  1. **Declaring a saga** — ``SagaConfig`` with ordered steps,
     per-step tools, compensation tools, skip conditions, and
     enrichment.
  2. **Start → dispatch** — ``saga.<name>.started`` dispatches
     the first non-skipped step's tool.
  3. **Advance** — a completed step advances the saga to the next
     non-skipped step.
  4. **Skip** — a step whose ``skip_when`` is satisfied is skipped.
  5. **Enrichment** — a step's params are enriched from the
     previous step's result (and the ``ContinuityComponent``).
  6. **Compensation** — a failed step rolls back the completed
     steps via their ``compensate_tool`` (LIFO).
  7. **Completion** — the last step emits ``saga.<name>.completed``.
  8. **SagaProjection** — the ``SagaProgressComponent`` is
     materialised from the saga events (ADR-069 §9.2 item 6).

## Run with

    KNT_REDIS_FAKE=1 uv run python examples/24_workflow_saga.py

The example is pure: it builds a ``World`` via the SUT builders
in ``kntgraph.testing`` and calls the ``SagaSystem`` against it.
No Redis, no dispatcher, no external model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

from kntgraph.concordos.saga import (
    SagaConfig,
    SagaProgressComponent,
    SagaProjection,
    SagaStepConfig,
    SagaSystem,
)
from kntgraph.concordos.specs import StepTimedOut
from kntgraph.core.event import Event
from kntgraph.core.event.correlation import CorrelationContext
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


def _progress(
    *,
    current_step: str,
    direction: str = "forward",
    step_states: dict[str, str] | None = None,
    step_results: dict[str, object] | None = None,
    compensate_stack: tuple[str, ...] = (),
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
        started_at=FIXED_NOW,
    )


def _saga_config() -> SagaConfig:
    """The NF-e emission saga from ADR-069 §4.8 (simplified)."""
    return SagaConfig(
        name="nfe_emission",
        saga_timeout_ms=300_000,
        # Default: fail on the first step failure (compensate).
        fail_when=None,
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


def _banner(msg: str) -> None:
    print("\n" + "=" * 72)
    print(msg)
    print("=" * 72)


def _run(
    *,
    current_step: str,
    trigger: str,
    step_states: dict[str, str] | None = None,
    step_results: dict[str, object] | None = None,
    compensate_stack: tuple[str, ...] = (),
    tool_name: str | None = None,
    completion: ToolCallCompletion | None = None,
) -> list[str]:
    """Build a World with one agent and run the saga against it.

    Returns the emitted event types.
    """
    builder = (
        AgentViewBuilder("agent-1")
        .with_component(
            _progress(
                current_step=current_step,
                step_states=step_states,
                step_results=step_results,
                compensate_stack=compensate_stack,
            )
        )
        .with_trigger(trigger)
    )
    if tool_name is not None:
        req_eid = str(uuid4())
        builder = builder.with_tool_request(_request(req_eid, tool_name))
        if completion is not None:
            builder = builder.with_tool_completion(req_eid, completion)
    view = builder.build()
    world = WorldBuilder().with_agent(view).build()
    out = run_system(SagaSystem(_saga_config(), now=lambda: FIXED_NOW), world)
    return [e.event_type for e in out]


def main() -> None:
    print("=== WorkflowSaga — orchestrate a sequence of tool calls (ADR-069 §4) ===")

    # ------------------------------------------------------------------
    # 1. Start → dispatch the first step.
    # ------------------------------------------------------------------
    _banner("1. saga.started → dispatch validate_fiscal")
    types = _run(
        current_step="validate_fiscal",
        trigger="saga.nfe_emission.started",
        step_states={"validate_fiscal": "pending"},
    )
    print(f"  events: {types}")
    assert "tool.sefaz_validator.requested" in types

    # ------------------------------------------------------------------
    # 2. Completed step → advance to the next step.
    # ------------------------------------------------------------------
    _banner("2. validate_fiscal completed → dispatch emit_nfe")
    types = _run(
        current_step="validate_fiscal",
        trigger="tool.sefaz_validator.completed",
        step_states={"validate_fiscal": "in_flight"},
        tool_name="sefaz_validator",
        completion=ToolCallCompletion(
            request_event_id="x",
            status="completed",
            result={"nfe_required": True, "cfop": "5102", "tax_amount": 100.0},
        ),
    )
    print(f"  events: {types}")
    assert "tool.nfe_emitter.requested" in types

    # ------------------------------------------------------------------
    # 3. Last step completed → saga completed.
    # ------------------------------------------------------------------
    _banner("3. register_receivable completed → saga.completed")
    types = _run(
        current_step="register_receivable",
        trigger="tool.erp_receivable_tool.completed",
        step_states={
            "validate_fiscal": "completed",
            "emit_nfe": "completed",
            "register_receivable": "in_flight",
        },
        tool_name="erp_receivable_tool",
        completion=ToolCallCompletion(
            request_event_id="x",
            status="completed",
            result={"receivable_id": "r-1"},
        ),
    )
    print(f"  events: {types}")
    assert "saga.nfe_emission.completed" in types

    # ------------------------------------------------------------------
    # 4. Failed step → compensate (LIFO).
    # ------------------------------------------------------------------
    _banner("4. emit_nfe failed → compensate via nfe_canceller")
    types = _run(
        current_step="emit_nfe",
        trigger="tool.nfe_emitter.failed",
        step_states={"validate_fiscal": "completed", "emit_nfe": "failed"},
        step_results={
            "validate_fiscal": {"nfe_required": True},
            "emit_nfe": {},
        },
        compensate_stack=("validate_fiscal", "emit_nfe"),
        tool_name="nfe_emitter",
        completion=ToolCallCompletion(
            request_event_id="x",
            status="failed",
            error="se_faz_offline",
        ),
    )
    print(f"  events: {types}")
    assert "tool.nfe_canceller.requested" in types

    # ------------------------------------------------------------------
    # 5. SagaProjection materialises the progress component.
    # ------------------------------------------------------------------
    _banner("5. SagaProjection materialises SagaProgressComponent")
    started = Event.create(
        agent_id="agent-1",
        event_type="saga.nfe_emission.started",
        event_class="domain",
        data={"saga_id": "saga-001"},
        correlation=CorrelationContext.new(),
    )
    view = AgentViewBuilder("agent-1").build()
    world = WorldBuilder().with_agent(view).build()
    new_world = SagaProjection(_saga_config())(world, [started])
    saga = new_world.get_agent("agent-1").get_component(SagaProgressComponent)
    print(f"  saga: {saga.saga_name} direction={saga.direction}")
    assert saga is not None
    assert saga.saga_name == "nfe_emission"
    assert saga.direction == "forward"

    print("\nAll saga scenarios passed.")


if __name__ == "__main__":
    main()
