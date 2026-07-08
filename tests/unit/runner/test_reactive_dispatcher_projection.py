# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``ReactiveDispatcher._fold_with_filter``
applied projection (ADR-036 §2.3).

The dispatcher must overlay ``project_tool_calls`` on
top of the post-fold World whenever the batch contains
a ``tool.*`` event, so systems that use
``ToolAwareSystem`` see ``tool_requests`` and
``tool_completions`` slots without any subclassing
or manual wiring.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4


from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.runner.reactive import ReactiveDispatcher


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid4())


class _FakeEventLog:
    """No-op EventLog stand-in (the projection overlay
    does not touch the log).
    """

    async def append_batch(self, events: list[Event]) -> Any:
        return ["ok"] * len(events)


def _request_event(agent_id: str = "a-1") -> Event:
    return Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "pii_redactor", "params": {"text": "x"}},
        correlation=_ctx(),
    )


def _completion_event(causation_id: Any, agent_id: str = "a-1") -> Event:
    return Event.create(
        event_type="tool.pii_redactor.completed",
        agent_id=agent_id,
        event_class="domain",
        data={"redacted": "***"},
        causation_id=causation_id,
        correlation=_ctx(),
    )


def _domain_event(agent_id: str = "a-1") -> Event:
    return Event.create(
        event_type="user.intent",
        agent_id=agent_id,
        event_class="domain",
        data={"intent": "get_weather"},
        correlation=_ctx(),
    )


def _dispatcher() -> ReactiveDispatcher:
    return ReactiveDispatcher(
        log=_FakeEventLog(),  # type: ignore[arg-type]
        systems=[],
        redis=AsyncMock(),
    )


def test_fold_with_tool_requested_materialises_request_slot() -> None:
    """A batch with a ``tool.requested`` event surfaces
    a ``ToolCallRequest`` in the agent's view after the
    fold. This is what ``ToolAwareSystem.has_requested``
    consumes.
    """
    d = _dispatcher()
    req = _request_event()

    world, _ = d._fold_with_filter(World.empty(), [req])

    view = world.views["a-1"]
    assert "tool_requests" in view.components
    assert str(req.event_id) in view.components["tool_requests"]


def test_fold_with_completion_materialises_completion_slot() -> None:
    """A batch with a ``tool.<name>.completed`` event
    surfaces a ``ToolCallCompletion`` linked to the
    request via ``causation_id``.
    """
    d = _dispatcher()
    req = _request_event()
    completion = _completion_event(causation_id=req.event_id)

    world, _ = d._fold_with_filter(World.empty(), [req, completion])

    view = world.views["a-1"]
    completions = view.components["tool_completions"]
    assert str(req.event_id) in completions
    assert completions[str(req.event_id)].status == "completed"
    assert completions[str(req.event_id)].result == {"redacted": "***"}


def test_fold_without_tool_events_does_not_materialise_slots() -> None:
    """A batch with no ``tool.*`` events must not touch
    the tool slots; the post-fold World looks the same
    as if no overlay had run. This is the cost-free
    fast path for non-tool batches.
    """
    d = _dispatcher()
    dom = _domain_event()

    world, _ = d._fold_with_filter(World.empty(), [dom])

    view = world.views["a-1"]
    assert "tool_requests" not in view.components
    assert "tool_completions" not in view.components


def test_fold_preserves_tick() -> None:
    """The tool overlay is a post-fold step, not a fold
    step: it must NOT advance ``world.tick`` beyond what
    ``with_event`` produced. Otherwise the runner's
    checkpoint cursor would desync.
    """
    d = _dispatcher()
    req = _request_event()
    dom = _domain_event()

    world, _ = d._fold_with_filter(World.empty(), [dom, req])

    # Two ``with_event`` calls -> tick advanced by 2.
    assert world.tick == 2


def test_fold_passes_through_views_without_tool_events() -> None:
    """When a batch has tool events for some agents and
    not for others, the agents without tool events
    keep their original view object (no allocation).
    This is the fast path for mixed batches.
    """
    d = _dispatcher()
    tool_a = _request_event(agent_id="a-1")
    domain_b = _domain_event(agent_id="a-2")

    world, _ = d._fold_with_filter(World.empty(), [tool_a, domain_b])

    # Agent a-1 has the tool slot installed.
    assert "tool_requests" in world.views["a-1"].components
    # Agent a-2 has no tool events -> the projection
    # left its view untouched (no tool slots).
    assert "tool_requests" not in world.views["a-2"].components
    assert "tool_completions" not in world.views["a-2"].components
