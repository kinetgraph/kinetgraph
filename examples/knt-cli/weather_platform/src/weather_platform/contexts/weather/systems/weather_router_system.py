# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from kntgraph.core.world import World
from kntgraph.core.event import Event, correlation_middleware
from kntgraph.tools.system import ToolAwareSystem
from ..events.weather_resolved import weather_resolved


def weather_router_system(world: World) -> list[Event]:
    """
    WorldSystem checking agent views for weather requests and completions.

    This system illustrates the two architectural paths supported by Kinetgraph:

    ===========================================================================
    PATH A: Direct Tool Invocation (Client-Driven / CLI / API)
    ===========================================================================
    If the external client calls the gateway with a direct tool invocation request
    (e.g., POST /agents/{agent_id}/intents with type="tool.invoke" and tool="open_meteo_api"),
    the gateway publishes `tool.open_meteo_api.requested` directly to the EventLog.
    The WorkerManager executes it, and emits the completion.

    -> Under Path A, this system is bypassed entirely for the request phase.

    ===========================================================================
    PATH B: System-Mediated Flow (Agent-Driven / Reactive ECS)
    ===========================================================================
    If the client publishes a general domain intent event (e.g., `weather.get_weather`
    carrying the city, latitude, and longitude), this system is triggered:
      1. It detects the `weather.get_weather` domain phase.
      2. It requests the tool `open_meteo_api` idempotently via `ToolAwareSystem`.
      3. On a subsequent tick, once the tool execution finishes, it detects the
         `tool.open_meteo_api.completed` phase.
      4. It extracts the raw result and emits a clean domain event: `weather.weather_resolved`.
    """
    events = []
    helper = ToolAwareSystem()

    for agent_id, view in world.views.items():
        # --- PATH B: Step 1 & 2 - Detect Intent Event and Request Tool ---
        is_intent = False
        intent_data = {}
        correlation_id = None

        if view.domain_phase == "weather.get_weather":
            intent_data = view.components.get("weather.get_weather") or {}
            is_intent = True
        elif view.domain_phase == "tool.weather_agent.requested":
            req = helper.get_request(view, view.last_event_id)
            if req:
                intent_data = req.params.get("args") or {}
                correlation_id = req.correlation_id
                is_intent = True

        if is_intent and view.last_event_id:
            city = intent_data.get("city", "Unknown")
            lat = intent_data.get("latitude")
            lon = intent_data.get("longitude")
            if lat is None or lon is None:
                continue

            # Propagate the correlation context via middleware
            correlation = correlation_middleware.start(correlation_id=correlation_id)

            # Request the tool. Since causation_id is stable (the ID of the intent event),
            # `request_tool` produces a stable, deterministic event_id.
            req_event = helper.request_tool(
                agent_id=agent_id,
                tool_name="open_meteo_api",
                params={
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "city": city,  # Carry the city parameter to correlate it in the result
                },
                causation_id=view.last_event_id,
                correlation=correlation,
            )

            # Check if this tool request has already been appended to the stream
            if not helper.has_requested(view, str(req_event.event_id)):
                events.append(req_event)

        # --- PATH B: Step 3 & 4 - Detect Tool Completion and Resolve Weather ---
        elif view.domain_phase == "tool.open_meteo_api.completed":
            tool_completions = view.components.get("tool_completions", {})
            for req_id, completion in tool_completions.items():
                req = helper.get_request(view, req_id)
                if (
                    req
                    and req.tool_name == "open_meteo_api"
                    and completion.status == "completed"
                ):
                    # Retrieve the parameters (like city name) from the request
                    city = req.params.get("params", {}).get("city") or "Unknown"

                    # Extract the raw weather data returned by the tool
                    weather_data = completion.result or {}
                    temp = weather_data.get("temperature", 0.0)
                    wind = weather_data.get("windspeed", 0.0)

                    # Propagate the correlation context of the tool completion
                    correlation_middleware.start(
                        correlation_id=completion.correlation_id
                    )

                    # Emit the clean domain event representing the resolved weather.
                    # This updates `domain_phase` to `weather.weather_resolved`, stopping the loop.
                    resolved_event = weather_resolved(
                        agent_id=agent_id,
                        data={
                            "city": city,
                            "temperature_celsius": float(temp),
                            "condition": f"Windspeed: {wind} km/h",
                        },
                        causation_id=completion.request_event_id,
                    )
                    events.append(resolved_event)
                    break

    return events
