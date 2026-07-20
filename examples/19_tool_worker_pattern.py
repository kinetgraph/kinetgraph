# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Example 19: Tool Worker Pattern (ADR-036)

This example demonstrates the decoupled architecture for executing I/O-bound
tools asynchronously using the ECS pattern and Redis Streams.

Key concepts demonstrated:
1. @tool_worker: Decorating a function to be executed by a worker.
2. ToolAwareSystem: A pure WorldSystem that requests a tool and reacts to its completion.
3. ToolRouter: Injected into the ReactiveDispatcher to fan-out `tool.requested` events.
4. WorkerManager: Consumes the global tool queue and executes the requested tools.

Architecture:
  [System] -> (emits tool.requested) -> [EventLog]
  [EventLog] -> (ToolRouter fan-out) -> [Redis Queue]
  [Redis Queue] -> (WorkerManager) -> [Tool Execution] -> (emits tool.<name>.completed)

Since ADR-036 was accepted, the dispatcher applies
``project_tool_calls`` by default in ``_fold_with_filter``,
so no subclassing is required to expose the
``tool_requests`` / ``tool_completions`` slots to
systems that use ``ToolAwareSystem``.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from kntgraph.core.event import (
    CorrelationContext,
    Event,
    correlation_middleware,
)
from kntgraph.core.world import World
from kntgraph.infra.redis import RedisEventLogAdapter
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter
from kntgraph.tools.system import ToolAwareSystem
from kntgraph.tools.worker import tool_worker

from _lib.redis_or_fake import make_redis_client

from kntgraph.core.result import Ok, Result, ToolError


# 1. Define a tool using the @tool_worker decorator
@tool_worker(name="weather_api", description="Fetches the weather for a city.")
class WeatherTool:
    """A mock I/O bound tool that simulates a network request."""

    async def invoke(
        self, city: str, *, idempotency_key: str
    ) -> Result[dict[str, Any], ToolError]:
        print(
            f"    [Worker] 🌩️ Fetching weather for {city} (idempotency={idempotency_key})..."
        )
        await asyncio.sleep(1.0)  # Simulate I/O

        if city.lower() == "london":
            return Ok({"temperature": 15, "condition": "Rainy"})
        elif city.lower() == "rio":
            return Ok({"temperature": 32, "condition": "Sunny"})

        return Ok({"temperature": 20, "condition": "Cloudy"})


# 2. Create a pure Domain System using ToolAwareSystem helper
class WeatherSystem(ToolAwareSystem):
    """
    A pure system that observes agents asking for weather.
    It doesn't block. It just emits a `tool.requested` event, and when
    it sees the `tool.completed` event in the World, it processes the result.

    The system holds a reference to the EventLog so it
    can fetch the triggering `Event` from the
    `last_event_id` stored on the agent's view and
    propagate its `correlation` to every downstream
    event (ADR-037). Without this, the
    ``ReactiveDispatcher``'s loop runs in a separate
    asyncio task whose ContextVar is independent from
    the main coroutine.
    """

    def __init__(self, event_log: EventLog) -> None:
        self._event_log = event_log

    def __call__(self, world: World) -> list[Event]:
        events = []

        for agent_id, view in world.views.items():
            # We assume the agent's intent is stored in its domain payload
            # e.g., the last domain event had data={"intent": "get_weather", "city": "Rio"}
            domain_data = view.components.get("user.intent", {})
            if domain_data.get("intent") != "get_weather":
                continue

            city = domain_data.get("city")
            if not city:
                continue

            # We need a deterministic request ID for idempotency and join-keys.
            # Typically, we use the agent's last domain event ID as the causation_id.
            causation_id = view.last_event_id
            if not causation_id:
                continue

            # ADR-037: the dispatcher runs in a separate
            # task whose ContextVar is independent. Read
            # the triggering Event from the EventLog to
            # recover the correlation. The `EventLog.read`
            # helper is async; here we use the
            # synchronous in-memory store view via
            # `world.views` to derive the correlation
            # without a roundtrip.
            trigger_correlation = (
                correlation_middleware.current()
                or CorrelationContext.new(correlation_id=UUID(causation_id))
            )

            # ADR-034 / ADR-036: Check ECS components for Tool state
            if not self.has_requested(view, causation_id):
                print(
                    f"  [System] 📡 Requesting weather tool for '{city}' (causation={causation_id})"
                )
                # This generates a `tool.requested` event.
                events.append(
                    self.request_tool(
                        agent_id=agent_id,
                        tool_name="weather_api",
                        params={"city": city},
                        causation_id=causation_id,
                        correlation=trigger_correlation,
                    )
                )
            elif self.is_pending(view, causation_id):
                print("  [System] ⏳ Weather tool is pending... doing nothing.")
            else:
                # The tool has completed!
                completion = self.get_completion(view, causation_id)
                # Check if we already reacted to this completion to avoid infinite loops
                # (In a real app, you'd migrate the agent's domain phase)
                if view.domain_phase != "weather_resolved":
                    print(f"  [System] ✅ Weather resolved: {completion.result}")
                    events.append(
                        Event.create(
                            event_type="weather_resolved",
                            agent_id=agent_id,
                            event_class="domain",
                            data={"weather": completion.result},
                            causation_id=causation_id,
                            correlation=trigger_correlation,
                        )
                    )

        return events


async def main():
    logging.basicConfig(level=logging.WARNING)
    print("=== Tool Worker Pattern Example ===")

    # Setup infrastructure
    redis_client = make_redis_client()
    await redis_client.flushdb()

    event_log = EventLog(RedisEventLogAdapter(redis_client))

    # 3. Setup the ReactiveDispatcher with the ToolRouter (Fan-Out).
    # project_tool_calls is applied by default in _fold_with_filter
    # (ADR-036 §2.3) -- no subclassing is needed.
    tool_router = ToolRouter(redis_client)
    dispatcher = ReactiveDispatcher(
        log=event_log,
        systems=[WeatherSystem(event_log)],
        redis=redis_client,
        tool_router=tool_router,
        poll_interval=0.5,
    )

    # 4. Setup the WorkerManager with the registered tool
    worker_manager = WorkerManager(
        redis=redis_client,
        event_log=event_log,
    )
    worker_manager.register(WeatherTool)

    print("\nStarting Dispatcher and Worker...")
    await dispatcher.start()
    await worker_manager.start()

    # 5. Trigger the flow by emitting a domain event
    agent_id = "agent-rio-1"
    print("\n[Client] Emitting 'get_weather' intent event...")
    # Open a correlation context for the whole flow
    # (ADR-037). The dispatcher and worker run in
    # background tasks; the context is set up here and
    # cleared AFTER the workers stop. ``current()`` is
    # per-task (ContextVar), so the system callback
    # reads the same context via ``correlation_middleware.current()``
    # because asyncio propagates ContextVars across
    # ``await`` boundaries within the same task.
    correlation_middleware.start(metadata={"example": "19"})
    try:
        await event_log.append(
            Event.create(
                event_type="user.intent",
                agent_id=agent_id,
                event_class="domain",
                data={"intent": "get_weather", "city": "Rio"},
                correlation=correlation_middleware.current(),
            )
        )

        # Wait for the system to process:
        # 1. System sees intent, emits tool.requested
        # 2. Router copies to tool queue
        # 3. Worker consumes, runs I/O, emits tool.completed
        # 4. System sees tool.completed, emits weather_resolved
        await asyncio.sleep(2.0)

        print("\nStopping components...")
        await dispatcher.stop()
        await worker_manager.stop()
    finally:
        correlation_middleware.clear()

    # Verify the EventLog
    print("\n=== Final Event Log for Agent ===")
    events = await event_log.read(agent_id)
    for e in events:
        print(f" - {e.event_type} | {e.data}")

    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
