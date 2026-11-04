# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``kntgraph.testing.concordo_harness`` (ADR-089).

Verifies the contract checkers for:
  - FSMContractVerifier (graph reachability, purity, dead-end detection).
  - SagaContractVerifier (step uniqueness, context dependencies, LIFO compensation).
  - AdapterContractVerifier (exception containment, cancellation propagation).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from result import Err, Ok

from kntgraph.concordos.base import Specification, StepContext
from kntgraph.concordos.fsm import FSMConfig, FSMTransition
from kntgraph.concordos.saga import SagaConfig, SagaStepConfig
from kntgraph.core.world import DomainComponent
from kntgraph.testing.concordo_harness import (
    AdapterContractVerifier,
    AdapterContractViolation,
    FSMContractVerifier,
    FSMContractViolation,
    SagaContractVerifier,
    SagaContractViolation,
)


@dataclass(frozen=True, slots=True)
class OrderStateComponent(DomainComponent):
    status: str = "created"


class AlwaysTrueSpec(Specification):
    def is_satisfied_by(self, context: StepContext) -> bool:
        return True


# ---------------------------------------------------------------------------
# FSMContractVerifier Tests
# ---------------------------------------------------------------------------


def test_fsm_valid_graph_passes() -> None:
    config = FSMConfig(
        component_type=OrderStateComponent,
        state_field="status",
        transitions={
            "created": {
                "order.pay": FSMTransition(to="paid", guard=AlwaysTrueSpec()),
                "order.cancel": FSMTransition(to="cancelled"),
            },
            "paid": {
                "order.ship": FSMTransition(to="shipped"),
            },
            "shipped": {
                "order.deliver": FSMTransition(to="delivered"),
            },
        },
        terminal=frozenset({"cancelled", "delivered"}),
    )

    # Must pass without raising
    FSMContractVerifier.verify_graph(config, initial_state="created")


def test_fsm_unreachable_state_detected() -> None:
    config = FSMConfig(
        component_type=OrderStateComponent,
        state_field="status",
        transitions={
            "created": {
                "order.pay": FSMTransition(to="paid"),
            },
            "paid": {
                "order.ship": FSMTransition(to="shipped"),
            },
            "orphan_state": {  # Unreachable!
                "order.retry": FSMTransition(to="paid"),
            },
        },
        terminal=frozenset({"shipped"}),
    )

    with pytest.raises(FSMContractViolation, match="Unreachable state.*orphan_state"):
        FSMContractVerifier.verify_graph(config, initial_state="created")


def test_fsm_dead_end_state_detected() -> None:
    config = FSMConfig(
        component_type=OrderStateComponent,
        state_field="status",
        transitions={
            "created": {
                "order.pay": FSMTransition(to="paid"),
            },
            "paid": {},  # Dead end, but not in terminal!
        },
        terminal=frozenset({"cancelled"}),
    )

    with pytest.raises(FSMContractViolation, match="Dead-end state 'paid'"):
        FSMContractVerifier.verify_graph(config, initial_state="created")


def test_fsm_invalid_guard_type_detected() -> None:
    config = FSMConfig(
        component_type=OrderStateComponent,
        state_field="status",
        transitions={
            "created": {
                "order.pay": FSMTransition(
                    to="paid",
                    guard="not_a_specification",  # type: ignore[arg-type]
                ),
            },
            "paid": {},
        },
        terminal=frozenset({"paid"}),
    )

    with pytest.raises(
        FSMContractViolation, match="must implement Specification protocol"
    ):
        FSMContractVerifier.verify_graph(config, initial_state="created")


# ---------------------------------------------------------------------------
# SagaContractVerifier Tests
# ---------------------------------------------------------------------------


def test_saga_step_verification_passes() -> None:
    config = SagaConfig(
        name="checkout",
        steps=(
            SagaStepConfig(name="charge", tool_name="stripe", compensate_tool="refund"),
            SagaStepConfig(
                name="reserve",
                tool_name="inventory",
                compensate_tool="release_stock",
                enrich_from=("steps.charge.output.txn_id",),
            ),
        ),
    )
    SagaContractVerifier.verify_step_configs(config)


def test_saga_duplicate_step_name_detected() -> None:
    config = SagaConfig(
        name="checkout",
        steps=(
            SagaStepConfig(name="charge", tool_name="stripe"),
            SagaStepConfig(name="charge", tool_name="paypal"),
        ),
    )
    with pytest.raises(SagaContractViolation, match="Duplicate step name 'charge'"):
        SagaContractVerifier.verify_step_configs(config)


def test_saga_forward_dependency_detected() -> None:
    config = SagaConfig(
        name="checkout",
        steps=(
            SagaStepConfig(
                name="charge",
                tool_name="stripe",
                enrich_from=("steps.reserve.output.stock_id",),  # Forward dependency!
            ),
            SagaStepConfig(name="reserve", tool_name="inventory"),
        ),
    )
    with pytest.raises(
        SagaContractViolation, match="references future or undeclared step 'reserve'"
    ):
        SagaContractVerifier.verify_step_configs(config)


def test_saga_lifo_compensation_simulation() -> None:
    config = SagaConfig(
        name="checkout",
        steps=(
            SagaStepConfig(name="step1", tool_name="tool_1", compensate_tool="undo_1"),
            SagaStepConfig(
                name="step2", tool_name="tool_2", compensate_tool=None
            ),  # No rollback needed
            SagaStepConfig(name="step3", tool_name="tool_3", compensate_tool="undo_3"),
            SagaStepConfig(name="step4", tool_name="tool_4", compensate_tool="undo_4"),
        ),
    )

    # If step 4 fails, steps 1, 2, 3 were completed.
    # Compensable steps were step 1 (undo_1) and step 3 (undo_3).
    # LIFO rollback order must be: undo_3, then undo_1.
    lifo_order = SagaContractVerifier.simulate_lifo_compensation(
        config, failed_step_name="step4"
    )
    assert lifo_order == ["undo_3", "undo_1"]

    # If step 1 fails, nothing prior was completed:
    assert (
        SagaContractVerifier.simulate_lifo_compensation(
            config, failed_step_name="step1"
        )
        == []
    )


# ---------------------------------------------------------------------------
# AdapterContractVerifier Tests
# ---------------------------------------------------------------------------


class CustomStorageError(Exception):
    pass


@pytest.mark.asyncio
async def test_adapter_exception_contained_passes() -> None:
    async def compliant_adapter_call() -> Any:
        return Err(CustomStorageError("Connection reset by peer"))

    # Must pass because error is properly contained in Result.err
    res = await AdapterContractVerifier.assert_exception_contained(
        compliant_adapter_call,
        expected_error_type=CustomStorageError,
    )
    assert res.is_err()


@pytest.mark.asyncio
async def test_adapter_raw_exception_escape_detected() -> None:
    async def leaky_adapter_call() -> Any:
        raise OSError("Raw socket connection lost")

    # Raw exception escaped: must be flagged as violation
    with pytest.raises(
        AdapterContractViolation,
        match="Raw third-party exception escaped adapter boundary",
    ):
        await AdapterContractVerifier.assert_exception_contained(
            leaky_adapter_call,
            expected_error_type=CustomStorageError,
        )


@pytest.mark.asyncio
async def test_adapter_cancellation_propagates_passes() -> None:
    async def cancelling_call() -> Any:
        raise asyncio.CancelledError()

    # CancelledError must propagate cleanly
    await AdapterContractVerifier.assert_cancellation_propagates(cancelling_call)


@pytest.mark.asyncio
async def test_adapter_cancellation_swallowing_detected() -> None:
    async def swallowing_call() -> Any:
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError:
            return Ok(None)  # Swallowed!

    with pytest.raises(
        AdapterContractViolation, match="swallowed asyncio.CancelledError"
    ):
        await AdapterContractVerifier.assert_cancellation_propagates(swallowing_call)
