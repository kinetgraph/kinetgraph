# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for ADR-037 correlation propagation inside the
``ReactiveDispatcher`` and ``Runner`` tick loops.

The bug (reported by the ``backoffice`` consumer via
``correlation_guard.py``): when a ``WorldSystem`` or
``CyclicSystem`` emits an event via
``Event.domain_from(..., correlation=correlation_middleware.current())``
inside a dispatcher/runner tick, ``current()`` returns ``None``
because neither loop binds a ``correlation_middleware.scope()``
around the system execution. The result is a ``TypeError`` from
``Event.create`` (ADR-037 enforcement).

These tests reproduce the crash WITHOUT the autouse
``reset_correlation_context`` fixture (which sets a default
context and masks the production bug). Each test explicitly
clears the contextvar before the tick to simulate the
production state.

The fix: ``Runner.tick_once`` and
``_systems_runner.run_systems_and_persist`` must open a
``correlation_middleware.scope()`` around the system execution
loop so ``current()`` returns a non-None context inside the
tick.
"""

from __future__ import annotations

from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio

from kntgraph.core.event import (
    CorrelationContext,
    Event,
    correlation_middleware,
)
from kntgraph.core.event.correlation import _correlation_context
from kntgraph.core.world import World
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.runner.runner import Runner
from kntgraph.stream.event_log import EventLog


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def clean_correlation():
    """Explicitly clear the correlation contextvar so the
    tick runs in the same state as production (no context
    bound by an outer scope or by the autouse conftest
    fixture)."""
    _correlation_context.set(None)
    yield
    _correlation_context.set(None)


def _seed_event(agent_id: str) -> Event:
    """Seed event with an explicit correlation so the
    EventLog append succeeds regardless of the
    contextvar state."""
    return Event.domain_from(
        agent_id=agent_id,
        type="test.seed",
        data={"n": 1},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


# ---------------------------------------------------------------------------
# Runner — CyclicSystem
# ---------------------------------------------------------------------------


class TestRunnerCorrelationInTick:
    async def test_system_can_read_correlation_inside_tick(self, clean_correlation):
        """A ``CyclicSystem`` that calls
        ``correlation_middleware.current()`` to build its
        emitted events must receive a non-None
        ``CorrelationContext``. Without the fix, the
        contextvar is empty and ``Event.domain_from`` raises
        ``TypeError`` (ADR-037)."""
        client = fakeredis.aioredis.FakeRedis(decode_responses=False)
        log = EventLog(RedisEventLogAdapter(client))

        def system(world: World) -> list[Event]:
            ctx = correlation_middleware.current()
            # The fix guarantees ctx is not None inside the tick.
            assert ctx is not None, (
                "correlation_middleware.current() returned None "
                "inside a Runner tick — the dispatcher must bind "
                "a scope around system execution (ADR-037)."
            )
            return [
                Event.domain_from(
                    agent_id="a-1",
                    type="test.emitted",
                    data={"ok": True},
                    correlation=ctx,
                )
            ]

        runner = Runner(log=log, cyclic_systems=[system])  # type: ignore[arg-type]
        # The tick must NOT raise.
        await runner.tick_once()

        # The emitted event landed in the log.
        events = await log.read("a-1")
        assert len(events) == 1
        assert events[0].event_type == "test.emitted"


# ---------------------------------------------------------------------------
# ReactiveDispatcher — WorldSystem
# ---------------------------------------------------------------------------


class TestReactiveDispatcherCorrelationInTick:
    async def test_system_can_read_correlation_inside_dispatch(self, clean_correlation):
        """A ``WorldSystem`` that calls
        ``correlation_middleware.current()`` inside a
        ``ReactiveDispatcher`` tick must receive a non-None
        ``CorrelationContext``."""
        client = fakeredis.aioredis.FakeRedis(decode_responses=False)
        log = EventLog(RedisEventLogAdapter(client))

        # Seed an event so the dispatcher has work to do.
        await log.append(_seed_event("a-1"))

        captured_ctx: list[object] = []

        def system(world: World) -> list[Event]:
            ctx = correlation_middleware.current()
            captured_ctx.append(ctx)
            if ctx is None:
                return []
            return [
                Event.domain_from(
                    agent_id="a-1",
                    type="test.reactive_emitted",
                    data={"ok": True},
                    correlation=ctx,
                )
            ]

        dispatcher = ReactiveDispatcher(
            log,
            systems=[system],  # type: ignore[arg-type]
            redis=client,
            poll_interval=0.01,
        )
        dispatcher.track_agent("a-1")
        await dispatcher.dispatch_once()

        # The system ran and saw a non-None context.
        assert len(captured_ctx) == 1
        assert captured_ctx[0] is not None, (
            "correlation_middleware.current() returned None "
            "inside a ReactiveDispatcher tick — the dispatcher "
            "must bind a scope around system execution (ADR-037)."
        )

        # The emitted event landed in the log.
        events = await log.read("a-1")
        types = [e.event_type for e in events]
        assert "test.reactive_emitted" in types
