# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""
kntgraph.testing.concordo_harness -- Architecture fitness test harness (ADR-089).

Deterministic contract test fixtures and invariant verifiers for:
  - BusinessFSMConcordo (ADR-071): Graph reachability, purity, zero-token governance.
  - WorkflowSagaConcordo (ADR-072): Step uniqueness, context dependency, LIFO rollback.
  - Storage/Service Adapters (ADR-047, ADR-077): Exception containment, cancellation.

Usage:
    from kntgraph.testing.concordo_harness import (
        FSMContractVerifier,
        SagaContractVerifier,
        AdapterContractVerifier,
    )
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from kntgraph.concordos.base import Specification

if TYPE_CHECKING:
    from kntgraph.concordos.fsm import FSMConfig, FSMTransition
    from kntgraph.concordos.saga import SagaConfig
    from kntgraph.core.event.event import Event
    from kntgraph.core.system import WorldSystem
    from kntgraph.core.world.world import World

T = TypeVar("T")
E = TypeVar("E", bound=Exception)

__all__ = [
    "AdapterContractVerifier",
    "AdapterContractViolation",
    "ContractViolation",
    "FSMContractVerifier",
    "FSMContractViolation",
    "SagaContractVerifier",
    "SagaContractViolation",
]


class ContractViolation(AssertionError):
    """Base error raised when a design invariant or contract is broken."""


class FSMContractViolation(ContractViolation):
    """Raised when an FSM violates ADR-071 architectural invariants."""


class SagaContractViolation(ContractViolation):
    """Raised when a WorkflowSaga violates ADR-072 architectural invariants."""


class AdapterContractViolation(ContractViolation):
    """Raised when an Adapter violates ADR-047 or ADR-077 invariants."""


def _validate_fsm_transition(
    curr: str,
    event_type: str,
    tr: FSMTransition,
    transitions: Mapping[str, Mapping[str, FSMTransition]],
    terminal: set[str],
) -> str:
    """Validate a single transition destination and guard contract."""
    dest = tr.to
    if dest not in transitions and dest not in terminal:
        raise FSMContractViolation(
            f"Transition from '{curr}' on event '{event_type}' "
            f"leads to unknown destination state '{dest}'."
        )
    if tr.guard is not None and not isinstance(tr.guard, Specification):
        raise FSMContractViolation(
            f"Transition guard for '{curr}' -> '{dest}' must implement "
            f"Specification protocol (ADR-069 §2)."
        )
    return dest


def _traverse_fsm_reachability(
    transitions: Mapping[str, Mapping[str, FSMTransition]],
    terminal: set[str],
    initial_state: str,
) -> set[str]:
    """Perform BFS reachability traversal and validate all encountered transitions."""
    visited: set[str] = set()
    queue: deque[str] = deque([initial_state])

    while queue:
        curr = queue.popleft()
        if curr in visited:
            continue
        visited.add(curr)

        if curr not in transitions:
            continue

        for event_type, tr in transitions[curr].items():
            dest = _validate_fsm_transition(curr, event_type, tr, transitions, terminal)
            if dest not in visited:
                queue.append(dest)

    return visited


def _assert_no_unreachable_states(
    transitions: Mapping[str, Mapping[str, FSMTransition]],
    visited: set[str],
    initial_state: str,
) -> None:
    """Ensure all declared states in transitions are reachable from initial_state."""
    declared_states = set(transitions.keys())
    unreachable = declared_states - visited
    if unreachable:
        sorted_unreachable = sorted(unreachable)
        raise FSMContractViolation(
            f"Unreachable state(s) detected in FSM: {sorted_unreachable}. "
            f"All non-terminal states must be reachable from initial state "
            f"'{initial_state}' (ADR-071)."
        )


def _assert_no_dead_end_states(
    transitions: Mapping[str, Mapping[str, FSMTransition]],
    terminal: set[str],
) -> None:
    """Ensure no state has empty transitions unless explicitly marked terminal."""
    for state, state_transitions in transitions.items():
        if not state_transitions and state not in terminal:
            raise FSMContractViolation(
                f"Dead-end state '{state}' has no outgoing transitions but is not "
                f"declared in terminal states {sorted(terminal)} (ADR-071 §3.1)."
            )


@dataclass(frozen=True, slots=True)
class FSMContractVerifier:
    """Verifies architectural and graph invariants for BusinessFSM (ADR-071)."""

    @staticmethod
    def verify_graph(config: FSMConfig, initial_state: str) -> None:
        """
        Validate graph reachability, dead-end detection, and guard contracts.

        Raises FSMContractViolation if:
          1. initial_state is not declared in transitions.
          2. Any state in transitions is unreachable from initial_state.
          3. Any state has no outgoing transitions and is not declared in terminal.
          4. Any transition destination state is undeclared.
          5. Any declared guard fails to implement the Specification protocol.
        """
        transitions = config.transitions
        terminal = set(config.terminal)

        if initial_state not in transitions:
            raise FSMContractViolation(
                f"Initial state '{initial_state}' is not defined in FSM transitions "
                f"(ADR-071 §3.1)."
            )

        visited = _traverse_fsm_reachability(transitions, terminal, initial_state)
        _assert_no_unreachable_states(transitions, visited, initial_state)
        _assert_no_dead_end_states(transitions, terminal)

    @staticmethod
    def verify_purity(
        system: WorldSystem,
        world: World,
        events: Sequence[Event] | None = None,
    ) -> list[Event]:
        """
        Verify that evaluating WorldSystem is strictly pure.

        Asserts that re-evaluating with identical World produces
        bit-for-bit identical emitted events without side effects.
        """
        if hasattr(system, "process_events") and events is not None:
            run_1 = system.process_events(world, list(events))  # type: ignore[attr-defined]
            run_2 = system.process_events(world, list(events))  # type: ignore[attr-defined]
        else:
            run_1 = system(world)
            run_2 = system(world)

        if not isinstance(run_1, list) or not isinstance(run_2, list):
            raise FSMContractViolation(
                f"WorldSystem must return list[Event], got {type(run_1).__name__}"
            )

        if len(run_1) != len(run_2):
            raise FSMContractViolation(
                f"FSMSystem is not deterministic: run 1 returned {len(run_1)} events, "
                f"run 2 returned {len(run_2)} events (ADR-071 §2)."
            )

        for i, (ev1, ev2) in enumerate(zip(run_1, run_2, strict=True)):
            if (
                ev1.event_type != ev2.event_type
                or ev1.agent_id != ev2.agent_id
                or ev1.data != ev2.data
            ):
                raise FSMContractViolation(
                    f"FSMSystem output divergence at event index {i}: "
                    f"{ev1.event_type} vs {ev2.event_type} (ADR-071 §2)."
                )

        return run_1


@dataclass(frozen=True, slots=True)
class SagaContractVerifier:
    """Verifies structural invariants and LIFO compensation (ADR-072)."""

    @staticmethod
    def verify_step_configs(config: SagaConfig) -> None:
        """
        Validate step uniqueness, non-empty tool names, and enrichment dependencies.

        Raises SagaContractViolation if:
          1. Step names are duplicate.
          2. Tool step has an empty string as tool_name.
          3. Step references output from a later or non-existent step in enrich_from.
        """
        seen_steps: set[str] = set()

        for step in config.steps:
            if step.name in seen_steps:
                raise SagaContractViolation(
                    f"Duplicate step name '{step.name}' in saga '{config.name}' "
                    f"(ADR-072 §2)."
                )

            # Check enrich_from dependencies
            for dep in step.enrich_from:
                # If dep specifies a step prefix like "steps.<step_name>.output.<field>"
                if dep.startswith("steps."):
                    parts = dep.split(".")
                    if len(parts) >= 2:
                        referenced_step = parts[1]
                        if referenced_step not in seen_steps:
                            raise SagaContractViolation(
                                f"Step '{step.name}' references future or undeclared "
                                f"step '{referenced_step}' in enrich_from: '{dep}'."
                            )

            seen_steps.add(step.name)

    @staticmethod
    def simulate_lifo_compensation(
        config: SagaConfig,
        failed_step_name: str,
    ) -> list[str]:
        """
        Simulate step failure and compute the exact LIFO compensation order.

        Returns the ordered list of compensate_tool names that must be invoked.
        Raises SagaContractViolation if failed_step_name is not found.
        """
        steps = config.steps
        completed_compensable_tools: list[str] = []
        found = False

        for step in steps:
            if step.name == failed_step_name:
                found = True
                break
            if step.compensate_tool:
                completed_compensable_tools.append(step.compensate_tool)

        if not found:
            raise SagaContractViolation(
                f"Failed step '{failed_step_name}' not found in saga '{config.name}'."
            )

        # LIFO order: reverse of completed steps with compensation tools
        expected_lifo = list(reversed(completed_compensable_tools))
        return expected_lifo


@dataclass(frozen=True, slots=True)
class AdapterContractVerifier:
    """Verifies exception translation and signal propagation on Adapters (ADR-077)."""

    @staticmethod
    async def assert_exception_contained(
        coro_factory: Callable[[], Awaitable[T]],
        expected_error_type: type[Exception],
    ) -> T:
        """
        Assert external exceptions are translated into typed Result[T, E].

        If the coroutine raises any raw third-party exception instead of returning
        a Result.err(expected_error_type), raises AdapterContractViolation.
        """
        try:
            result = await coro_factory()
        except Exception as exc:
            raise AdapterContractViolation(
                f"Raw third-party exception escaped adapter boundary: "
                f"{type(exc).__name__}: {exc}. Adapters must translate all exceptions "
                f"into Result.err({expected_error_type.__name__}) (ADR-077 §1.4)."
            ) from exc

        # Check if result is a Result object
        is_err_fn = getattr(result, "is_err", None)
        err_val_attr = getattr(result, "err_value", None)
        if callable(is_err_fn) and err_val_attr is not None:
            if not is_err_fn():
                raise AdapterContractViolation(
                    f"Expected adapter to return Err({expected_error_type.__name__}), "
                    f"but it returned Ok."
                )
            err = err_val_attr() if callable(err_val_attr) else err_val_attr
            if not isinstance(err, expected_error_type):
                raise AdapterContractViolation(
                    f"Adapter returned Err({type(err).__name__}), expected "
                    f"Err({expected_error_type.__name__}) (ADR-077 §2.1)."
                )

        return result

    @staticmethod
    async def assert_cancellation_propagates(
        coro_factory: Callable[[], Awaitable[T]],
    ) -> None:
        """
        Assert that asyncio.CancelledError is NOT caught or swallowed by the adapter.

        System runtime control signals must propagate cleanly (ADR-077 §2.3/§2.5).
        """
        try:
            await coro_factory()
        except asyncio.CancelledError:
            # Correct behavior: cancellation propagated
            return
        except Exception as exc:
            raise AdapterContractViolation(
                f"Adapter caught or transformed asyncio.CancelledError into "
                f"{type(exc).__name__}. Cancellation signals must propagate cleanly "
                f"(ADR-077 §2.5)."
            ) from exc

        raise AdapterContractViolation(
            "Adapter swallowed asyncio.CancelledError without re-raising "
            "(ADR-077 §2.5)."
        )
