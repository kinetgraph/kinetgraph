# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.saga._components -- WorkflowSaga ECS component (ADR-069 §4.4).

``SagaProgressComponent`` is the saga execution state. The
*execution* fields (saga_id, current_step, direction,
started_at) are written by saga events; the *history* fields
(step_states, step_results, compensate_stack) are derived
from the EventLog and carried as a cache for system reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from kntgraph.core.world.component import DomainComponent

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue

__all__ = ["SagaProgressComponent"]


@dataclass(frozen=True, slots=True)
class SagaProgressComponent(DomainComponent):
    """
    C-02: WorkflowSaga — saga execution state.

    Source of truth for the *execution* fields
    (``saga_id``, ``saga_name``, ``current_step``,
    ``direction``, ``started_at``). These are written when
    the corresponding ``saga.<name>.started`` /
    ``saga.<name>.compensating`` / etc. events are appended
    and the saga-system projection materialises the
    component.

    Source of truth for the *history* fields
    (``step_states``, ``step_results``, ``compensate_stack``)
    is the EventLog. The component carries them as a CACHE
    for system reads; a re-fold of the EventLog MUST
    reconstruct them deterministically via the projection
    (ADR-069 §9.2 item 6).

    Tool call state (in-flight, completed, failed) lives in
    ``ToolCallRequest`` / ``ToolCallCompletion`` (ADR-034) and
    is read from the agent's view — NOT duplicated here.
    """

    saga_id: str
    saga_name: str
    current_step: str
    direction: str
    # "forward" | "compensating" | "done" | "compensated" |
    # "compensation_failed"
    step_order: tuple[str, ...]  # declared order (immutable)
    step_states: MappingProxyType[str, str]
    # step_name -> "pending" | "skipped" | "in_flight"
    #              "completed" | "failed" | "timed_out"
    #              "compensated" | "compensation_failed"
    step_results: MappingProxyType[str, "JsonValue"]
    # step_name -> result dict from ToolCallCompletion.result
    compensate_stack: tuple[str, ...]  # LIFO; steps pending compensation
    started_at: datetime
