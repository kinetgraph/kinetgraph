# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``ReactiveDispatcher`` fan-out via ``ToolRouter``
(ADR-036 §2.5).

These tests bypass the full dispatch loop and exercise
``_append_system_outgoing`` in isolation: a fake ``EventLog``
captures the appended events and a spy ``ToolRouter`` records
the routed batch. The contract under test is:

  - When the dispatcher is constructed without a
    ``tool_router``, no fan-out is attempted (and no error
    is raised).
  - When a ``tool_router`` is provided, every batch emitted
    by a system is appended to the EventLog AND routed
    through the router. The append happens first; routing
    is best-effort transport on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.runner.reactive import ReactiveDispatcher


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid4())


@dataclass
class _CapturedCall:
    """Records ordering between ``append_batch`` and
    ``route_batch``. ``order`` is a list of (op, [event_types]).
    """

    order: list[tuple[str, list[str]]]


class _FakeEventLog:
    """Minimal stand-in for ``EventLog``.

    ``append_batch`` records its call into the shared
    ``capture`` so the test can assert the ordering
    with the router.
    """

    def __init__(self, capture: _CapturedCall) -> None:
        self._capture = capture
        self.appended: list[Event] = []

    async def append_batch(self, events: list[Event]) -> Any:
        self._capture.order.append(("append", [e.event_type for e in events]))
        self.appended.extend(events)
        return ["ok"] * len(events)


class _SpyToolRouter:
    """Spy for ``ToolRouter`` -- records every batch routed."""

    def __init__(self, capture: _CapturedCall) -> None:
        self._capture = capture
        self.calls: list[list[Event]] = []

    async def route_batch(self, events: list[Event]) -> None:
        self._capture.order.append(("route", [e.event_type for e in events]))
        self.calls.append(list(events))


def _request_event() -> Event:
    return Event.create(
        event_type="tool.requested",
        agent_id="a-1",
        event_class="domain",
        data={"tool": "pii_redactor", "params": {"text": "x"}},
        correlation=_ctx(),
    )


def _domain_event() -> Event:
    return Event.create(
        event_type="user.intent",
        agent_id="a-1",
        event_class="domain",
        data={"intent": "get_weather", "city": "Rio"},
        correlation=_ctx(),
    )


def _system_returning(events: list[Event]):
    """Build a sync ``WorldSystem`` that returns a fixed batch."""

    def _system(world: Any) -> list[Event]:
        return list(events)

    return _system


@pytest.mark.asyncio
async def test_no_router_does_not_attempt_fan_out() -> None:
    """When ``tool_router`` is None, ``_append_system_outgoing``
    only calls ``append_batch``. No fan-out is attempted.
    """
    capture = _CapturedCall(order=[])
    log = _FakeEventLog(capture)
    dispatcher = ReactiveDispatcher(
        log=log,  # type: ignore[arg-type]
        systems=[],
        redis=AsyncMock(),
    )

    assert dispatcher._tool_router is None
    await dispatcher._append_system_outgoing(world=None, agent_id="a-1")  # type: ignore[arg-type]

    assert capture.order == []
    assert log.appended == []


@pytest.mark.asyncio
async def test_router_receives_batch_after_append() -> None:
    """With a ``tool_router`` wired in, the route call
    happens AFTER ``append_batch``. Both see the same
    batch (same events, same order).
    """
    capture = _CapturedCall(order=[])
    log = _FakeEventLog(capture)
    router = _SpyToolRouter(capture)

    req = _request_event()
    dom = _domain_event()
    dispatcher = ReactiveDispatcher(
        log=log,  # type: ignore[arg-type]
        systems=[_system_returning([dom, req])],
        redis=AsyncMock(),
        tool_router=router,  # type: ignore[arg-type]
    )

    await dispatcher._append_system_outgoing(world=None, agent_id="a-1")  # type: ignore[arg-type]

    # append happened first, then route.
    assert capture.order == [
        ("append", ["user.intent", "tool.requested"]),
        ("route", ["user.intent", "tool.requested"]),
    ]
    # The router received the same batch the log got.
    assert [e.event_type for e in router.calls[0]] == [
        "user.intent",
        "tool.requested",
    ]


@pytest.mark.asyncio
async def test_router_only_sees_tool_requested_when_only_those_emit() -> None:
    """A system that emits only a domain event should not
    trigger the router (no tool.requested in the batch).
    The router is still called -- ToolRouter filters inside.
    """
    capture = _CapturedCall(order=[])
    log = _FakeEventLog(capture)
    router = _SpyToolRouter(capture)

    dom = _domain_event()
    dispatcher = ReactiveDispatcher(
        log=log,  # type: ignore[arg-type]
        systems=[_system_returning([dom])],
        redis=AsyncMock(),
        tool_router=router,  # type: ignore[arg-type]
    )

    await dispatcher._append_system_outgoing(world=None, agent_id="a-1")  # type: ignore[arg-type]

    # Append happened; router was called with the same batch
    # (ToolRouter.route_batch internally filters tool.requested).
    assert [op for op, _ in capture.order] == ["append", "route"]
    assert [e.event_type for e in router.calls[0]] == ["user.intent"]
