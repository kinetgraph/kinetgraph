# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end integration test for the Tool Worker Pattern
(ADR-036). This is the canonical "happy path":

  ReactiveDispatcher
    + tool_router=ToolRouter(redis)
    + systems=[WeatherSystem]  (a ToolAwareSystem)
        |
        v  (emits tool.requested)
    EventLog (append)
        |
        v  (fan-out: route_batch)
    knt:tools:weather:queue  (Redis Stream)
        |
        v  (WorkerManager._consume_loop)
    WeatherTool.invoke(...)  (in ProcessPoolExecutor)
        |
        v  (emits tool.weather.completed)
    EventLog (append)  ->  World has tool_completions slot

This test exercises the whole stack against a real
Redis (db 15, flushed per test). It is the regression
gate for the wiring done in Steps 2-4: if any layer
breaks (ToolRouter fan-out, project_tool_calls overlay,
WorkerManager consumer loop, or the @tool_worker
contract), this test fails.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Ok, Result
from kntgraph.core.world import World
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools import (
    ToolAwareSystem,
    ToolRouter,
    WorkerManager,
    tool_worker,
)
from uuid import uuid4


pytestmark = pytest.mark.asyncio


# Module-level so the class can be pickled by the
# ProcessPoolExecutor in WorkerManager.
@tool_worker(name="weather", description="Fetches the weather for a city.")
class WeatherTool:
    """Sync-friendly I/O simulation: returns a fixed
    payload. ``idempotency_key`` is required by
    ``@tool_worker``; the WorkerManager passes the
    request's event_id as the key.
    """

    async def invoke(
        self,
        city: str,
        *,
        idempotency_key: str,
    ) -> Result[dict, Exception]:
        # Sanity-check: the key is the request's event_id.
        assert isinstance(idempotency_key, str)
        return Ok({"city": city, "temperature": 30})


class WeatherSystem(ToolAwareSystem):
    """Pure system: reads the ``user.intent`` component
    and emits a ``tool.requested`` event. Reacts to the
    completion on the next tick by emitting a
    ``weather_resolved`` event.

    The test uses the default projection
    (``{event_type: event.data}``), so each event
    overwrites the previous ``components`` slot.
    ``user.intent`` is the input component; the system
    reads it on the first tick, but on the second
    tick it has been replaced by ``tool.requested``.
    To know whether to react, the system looks at the
    ``tool_completions`` slot of the overlay rather
    than the intent.
    """

    def __init__(self, log):
        # ADR-037: the system needs the EventLog only
        # as a fallback for tests that bypass the
        # ``ToolCallCompletion.correlation_id`` slot
        # (e.g. the happy-path integration test).
        # In the canonical flow, the projection
        # materialises ``correlation_id`` on every
        # ``ToolCallRequest`` and ``ToolCallCompletion``,
        # so the system can read it from the World.
        self._log = log

    def __call__(self, world: World) -> list[Event]:
        events: list[Event] = []
        for agent_id, view in world.views.items():
            completions = view.components.get("tool_completions", {})
            requests = view.components.get("tool_requests", {})

            # If any tool completed, react to each.
            for request_event_id, comp in completions.items():
                if comp.status != "completed":
                    continue
                # Get the request to find the city. The
                # ``params`` slot of ToolCallRequest is
                # the full event data dict, so the
                # original ``params`` field is at
                # ``request.params["params"]``.
                request = requests.get(request_event_id)
                if request is None:
                    continue
                city = request.params.get("params", {}).get("city")
                if not city:
                    continue
                # Check the last domain phase to avoid
                # re-emitting weather_resolved on every
                # tick once it's been emitted.
                if view.domain_phase == "weather_resolved":
                    continue
                # ADR-037: the ``ToolCallCompletion``
                # slot now carries the flow's
                # ``correlation_id`` (via the
                # ``correlation_id`` field added in this
                # ADR). We read it directly instead of
                # going back to the log.
                flow_id = (
                    comp.correlation_id if comp.correlation_id is not None else uuid4()
                )
                events.append(
                    Event.create(
                        event_type="weather_resolved",
                        agent_id=agent_id,
                        event_class="domain",
                        data={"city": city, "result": comp.result},
                        causation_id=request_event_id,
                        correlation=CorrelationContext.new(correlation_id=flow_id),
                    )
                )
                return events

            # Otherwise, if the intent is present (first
            # tick) and no request is pending, emit one.
            intent = view.components.get("user.intent")
            if intent and intent.get("intent") == "get_weather":
                city = intent.get("city")
                if not city:
                    continue
                if not requests:
                    # First tick of a fresh flow: the
                    # system is the entry point of the
                    # ``weather`` tool call. Mint a new
                    # flow id. ADR-037: the caller (the
                    # system) MUST supply a correlation.
                    events.append(
                        self.request_tool(
                            agent_id=agent_id,
                            tool_name="weather",
                            params={"city": city},
                            causation_id=view.last_event_id,
                            correlation=CorrelationContext.new(correlation_id=uuid4()),
                        )
                    )
        return events


async def test_dispatcher_to_worker_end_to_end(clean_redis) -> None:
    """Full happy path: dispatcher emits a tool
    request, the router copies it to the tool queue,
    the WorkerManager picks it up, the tool runs,
    the completion event lands back in the agent's
    log, and the system sees the completion via the
    default ``overlay_tool_calls`` projection.
    """
    agent_id = f"a-{uuid.uuid4()}"
    adapter = RedisEventLogAdapter(clean_redis)
    log = EventLog(adapter)

    router = ToolRouter(clean_redis)
    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[WeatherSystem(log)],
        redis=clean_redis,
        tool_router=router,
    )
    dispatcher.track_agent(agent_id)

    worker_manager = WorkerManager(clean_redis, event_log=log)
    worker_manager.register(WeatherTool)

    try:
        # Trigger the flow by appending a user intent
        # directly to the agent's EventLog. This
        # bypasses any external producer and keeps
        # the test focused on the framework wiring.
        # ADR-037: the entry event of a flow needs
        # a ``CorrelationContext``. The test owns the
        # flow id; the system propagates it.
        flow_id = uuid4()
        await log.append(
            Event.create(
                event_type="user.intent",
                agent_id=agent_id,
                event_class="domain",
                data={"intent": "get_weather", "city": "Rio"},
                correlation=CorrelationContext.new(correlation_id=flow_id),
            )
        )

        # First tick: the system sees user.intent and
        # emits tool.requested. The tool_router fans
        # it out to knt:tools:weather:queue.
        await dispatcher.dispatch_once()

        # Verify the fan-out reached the tool queue.
        # The router should have xadd'd a message to
        # the queue with the full request payload.
        queue_len = await clean_redis.xlen("knt:tools:weather:queue")
        assert queue_len >= 1, (
            f"ToolRouter did not fan-out the request (queue len={queue_len})"
        )

        # Start the WorkerManager; it consumes the
        # message and runs the tool, emitting
        # tool.weather.completed back to the agent's log.
        await worker_manager.start()

        # Wait for the completion to land. Bounded
        # poll: 20 ticks of 100ms = 2s budget.
        completed_seen = False
        for _ in range(20):
            events = await log.read(agent_id)
            if any(e.event_type == "tool.weather.completed" for e in events):
                completed_seen = True
                break
            await asyncio.sleep(0.1)
        assert completed_seen, "WorkerManager did not emit tool.weather.completed"

        # Stop the worker before the next dispatcher
        # tick to avoid races with the next assertion.
        await worker_manager.stop()

        # Second tick: the system sees the completion
        # via overlay_tool_calls (projected by default)
        # and emits weather_resolved.
        await dispatcher.dispatch_once()

        events = await log.read(agent_id)
        event_types = [e.event_type for e in events]
        assert "weather_resolved" in event_types, (
            f"System did not react to the completion. Got events: {event_types}"
        )
    finally:
        if worker_manager._running:
            await worker_manager.stop()
