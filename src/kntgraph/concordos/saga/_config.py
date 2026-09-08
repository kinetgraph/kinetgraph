# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.saga._config -- WorkflowSaga configuration (ADR-069 §4.3).

A WorkflowSaga orchestrates a sequence of tool calls with
context enrichment, skip conditions, failure policies, and
compensation. The config is the only input that varies per
vertical; the saga system itself is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.concordos.base import Specification

__all__ = ["SagaConfig", "SagaStepConfig"]


@dataclass(frozen=True, slots=True)
class SagaStepConfig:
    """
    Configuration for a single saga step.

    ``name``             -- unique step identifier within the saga.
    ``tool_name``        -- registered @tool_worker name (ADR-036).
                            ``None`` declares a "human step"
                            (ADR-069 §9.2 item 3): the saga
                            blocks until an external
                            ``saga.<step>.approved`` event
                            arrives. Such steps get NO
                            ADR-045 TTL registration.
    ``compensate_tool``  -- @tool_worker called on rollback; None if
                            the step produces no compensable effect.
    ``skip_when``        -- Specification; step is skipped (not
                            dispatched) when satisfied.
    ``proceed_when``     -- Specification evaluated after step
                            completes; if not satisfied, treated as
                            failure even if tool returned success.
    ``compensate_when``  -- Specification evaluated when deciding
                            whether to compensate; None means always
                            compensate when rolling back.
    ``enrich_from``      -- field names to inject into tool params
                            before dispatch. Read from the previous
                            step's ``step_results``; if missing, the
                            field is omitted (no implicit ``None``).
    ``timeout_ms``       -- step-level timeout (passed to ADR-045
                            TTL registration on dispatch).
                            Ignored for human steps.
    """

    name: str
    tool_name: str | None
    compensate_tool: str | None = None
    skip_when: "Specification | None" = None
    proceed_when: "Specification | None" = None
    compensate_when: "Specification | None" = None
    enrich_from: tuple[str, ...] = ()
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        # Tool name must be non-empty when present.
        # ``None`` is the explicit signal for a human
        # step; an empty string is a typo.
        if self.tool_name is not None and not self.tool_name:
            raise ValueError(
                f"SagaStepConfig.tool_name must be a non-empty "
                f"string or None (human step); got empty string "
                f"for step {self.name!r}."
            )


@dataclass(frozen=True, slots=True)
class SagaConfig:
    """
    Configuration for a WorkflowSaga.

    ``name``            -- unique saga identifier.
    ``steps``           -- ordered tuple of step configs.
    ``fail_when``       -- Specification evaluated after every step
                           failure; saga fails when satisfied.
                           Default: fail on first REQUIRED step failure.
    ``saga_timeout_ms`` -- wall-clock timeout for the entire saga;
                           enforced by SagaTimeoutSystem (CyclicSystem).
    """

    name: str
    steps: tuple[SagaStepConfig, ...]
    fail_when: "Specification | None" = None  # None = fail on first failure
    saga_timeout_ms: int = 300_000
