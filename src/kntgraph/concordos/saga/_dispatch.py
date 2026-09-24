# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
concordos.saga._dispatch -- step dispatch + param enrichment.

Extracted from ``_system.py`` to keep that file under the
500-line guideline (ADR-069 §11.12). The dispatch logic
emits the per-step ``tool.<name>.requested`` (or
``saga.<step>.awaiting_approval`` for human steps) and
enriches the tool params from previous-step results and
the agent's ``ContinuityComponent`` (ADR-069 §9.2 item 2).

Two main functions:

  - ``dispatch_step``: emit the per-step dispatch event.
  - ``enrich_params``: fill the params dict from previous
    step results and continuity state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from kntgraph.core.components.memory import ContinuityComponent

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.event.event import Event
    from kntgraph.core.world.view import AgentView
    from kntgraph.concordos.base import ViewTrigger
    from kntgraph.concordos.saga._config import SagaStepConfig


__all__ = ["dispatch_step", "enrich_params"]


# Reuse the canonical ``_SagaSystemLike`` Protocol from
# ``_records.py`` (single source of truth).
from ._records import _SagaSystemLike  # noqa: F401


def dispatch_step(
    saga: _SagaSystemLike,
    view: "AgentView",
    step_config: "SagaStepConfig",
    trigger: "ViewTrigger",
) -> "Event":
    """
    Emit ``tool.<name>.requested`` for the step.

    Human steps (``tool_name is None``) are NOT dispatched
    via ``tool.<name>.requested``. Instead they emit
    ``saga.<step_name>.awaiting_approval`` and the saga
    blocks until a corresponding
    ``saga.<step_name>.approved`` /
    ``saga.<step_name>.rejected`` event arrives
    (ADR-069 §9.2 item 3).
    """
    from ._records import emit

    if step_config.tool_name is None:
        return emit(
            saga,
            trigger,
            event_type=(f"saga.{saga._cfg.name}.{step_config.name}.awaiting_approval"),
            data={"step_name": step_config.name},
        )
    params: dict[str, "JsonValue"] = {
        "saga_id": trigger.data.get("saga_id", ""),
    }
    enrich_params(saga, view, step_config, trigger, params)
    return emit(
        saga,
        trigger,
        event_type=f"tool.{step_config.tool_name}.requested",
        data=params,
    )


def enrich_params(
    saga: _SagaSystemLike,
    view: "AgentView",
    step_config: "SagaStepConfig",
    trigger: "ViewTrigger",
    params: dict[str, "JsonValue"],
) -> None:
    """Enrich the tool params from previous step results and
    the ``ContinuityComponent`` (ADR-069 §9.2 item 2).

    A field named in ``enrich_from`` is read first from the
    previous step results (via the trigger's data envelope),
    then from the agent's continuity state (last_tools /
    last_entities / last_categories).
    """
    previous = trigger.data.get("step_results", {})
    if isinstance(previous, Mapping):
        for field in step_config.enrich_from:
            for prev_result in previous.values():
                if isinstance(prev_result, Mapping) and field in prev_result:
                    params.setdefault(field, prev_result[field])
    continuity = view.get_component(ContinuityComponent)
    if continuity is not None:
        _enrich_from_continuity(step_config, continuity, params)


def _enrich_from_continuity(
    step_config: "SagaStepConfig",
    continuity: ContinuityComponent,
    params: dict[str, "JsonValue"],
) -> None:
    """Read ``enrich_from`` fields from the
    ``ContinuityComponent`` (last_tools / last_entities /
    last_categories) when they did not come from a previous
    step result.
    """
    for field in step_config.enrich_from:
        if field in params:
            continue
        if field in continuity.last_tools:
            params[field] = continuity.last_tools[field]
        elif field in continuity.last_entities:
            params[field] = continuity.last_entities[field]
        elif field in continuity.last_categories:
            params[field] = continuity.last_categories[field]
