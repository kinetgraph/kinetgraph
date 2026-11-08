# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
#
"""
concordos.saga._skip -- the step-skipping predicates.

A saga step may declare a ``skip_when`` Specification: when
the predicate is satisfied, the step does not run. The two
helpers here answer "which step runs next" by walking the
declared order and skipping the steps whose predicate says so.

Both helpers evaluate the predicate against the canonical
``StepContext``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._context import build_step_context
from ..base import StepContext

if TYPE_CHECKING:
    from kntgraph.core.world.view import AgentView
    from kntgraph.core.world.world import World

    from ._components import SagaProgressComponent
    from ._config import SagaStepConfig
    from ._records import _SagaSystemLike

__all__ = ["first_non_skipped_step", "next_non_skipped_step"]


def first_non_skipped_step(
    saga: _SagaSystemLike,
    progress: SagaProgressComponent,
    view: AgentView,
    world: World,
) -> SagaStepConfig | None:
    """Return the first step in declared order that is not
    skipped (per its ``skip_when`` Specification).

    The predicate sees the same memory tiers and domain
    components as an FSM guard.
    """
    ctx = build_step_context(
        view, world, saga._now(), progress.step_results, progress.step_states
    )
    for step_name in progress.step_order:
        step_cfg = saga._step_map.get(step_name)
        if step_cfg is None:
            continue
        if step_cfg.skip_when is not None and step_cfg.skip_when.is_satisfied_by(ctx):
            continue
        return step_cfg
    return None


def next_non_skipped_step(
    saga: _SagaSystemLike,
    current_step: SagaStepConfig,
    ctx: StepContext,
) -> SagaStepConfig | None:
    """Return the next step after ``current_step`` in declared
    order that is not skipped."""
    order = saga._cfg.steps
    try:
        idx = order.index(current_step)
    except ValueError:
        return None
    for step_cfg in order[idx + 1 :]:
        if step_cfg.skip_when is not None and step_cfg.skip_when.is_satisfied_by(ctx):
            continue
        return step_cfg
    return None
