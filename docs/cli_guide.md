<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Kinetgraph CLI Guide: Building a Weather Platform

The `knt` CLI is the official scaffolding tool for Kinetgraph. It strictly enforces Domain-Driven Design (DDD) and Kinetgraph's architectural standards (ADRs) by generating correctly structured boilerplate code.

In this guide, we will learn how to use the CLI by building a **Weather Platform**. We will scaffold a new project, create a bounded context, generate pure Systems and impure Tools, declare the agent, and run it behind a Reactive Dispatcher.

The CLI follows the **declarative agent model** ([ADR-088](../ADRs/ADR-088-Declarative-Agent-Definition-over-Concordos.md)): an agent is one `AgentDefinition` — persona, capability policy, state machine, workflows — and the framework derives the installable runtime from it. Two of the generated files (the agent declaration and the dispatcher) are *derived* from the context, so the CLI keeps them in sync as you add artifacts.

---

## Prerequisites

Make sure you have installed Kinetgraph with CLI support:

```bash
pip install "kntgraph[cli,api]"
```

*Note: The `[api]` extra installs FastAPI and Uvicorn, which we will use for our HTTP Gateway.*

---

## Phase 1: Project Initialization

The first step is to bootstrap our repository. The `init` command generates the standard directory structure, dependency files, and the main entry point.

```bash
uv run knt init project weather_platform --use-intent-http --routing-mode external
```

### What did this do?
- **`--use-intent-http`**: This flag tells the CLI to scaffold a `main.py` configured with FastAPI, `ToolRouter`, and `WorkerManager`. It wires up an HTTP Gateway that can receive intents from users.
- **`--routing-mode`**: This flag selects the intent-routing scaffold mode. Supported values are `external`, `autonomous`, and `collaborate`. The generated project includes a shared routing core (`components.py`, `policy.py`, `resolution.py`) plus mode-specific adapters under `src/<project>/routing/adapters/`.
  - `external`: intent entry comes from outside the agent boundary.
  - `autonomous`: the agent resolves the intent internally and proceeds without a collaboration layer.
  - `collaborate`: multiple agents or roles participate in resolving the same intent.
- Created `pyproject.toml` and injected `kntgraph[api]` and `uvicorn`.
- Created the core folder structure: `src/weather_platform/contexts/`.

Navigate into your new project:
```bash
cd weather_platform
uv sync
```

---

## Phase 2: The Bounded Context

Kinetgraph enforces Modular Monoliths. Code is never loosely floating; it belongs to a Bounded Context. Let's create a `weather` context.

```bash
uv run knt new context weather
```

### What did this do?
It generated `src/weather_platform/contexts/weather/dispatcher.py` and empty subdirectories for `agents`, `components`, `concordos`, `events`, `systems`, and `tools`.
The `dispatcher.py` file is the "brain" of this context. It registers the context's tools with the `WorkerManager` and installs the context's agent declarations on the `ReactiveDispatcher`.

> The `concordos/` directory holds the context's two execution cycles — the state machine and the workflows. The agent declaration references them (see [Phase 3.5](#phase-35-the-agent-declaration)).

---

## Phase 3: Building the Domain (DDD & ECS)

Now we will use the `knt new` group to generate the building blocks of our Weather Agent.

### 1. Components (State)
Components represent the immutable state in the ECS (Entity-Component-System) architecture. They are **domain components**: they derive from `DomainComponent`, and the agent's rules read them *by their concrete class*.

```bash
uv run knt new component weather.LocationIntent
```
*Open `src/weather_platform/contexts/weather/components/location_intent.py` and modify it:*
```python
from dataclasses import dataclass

from kntgraph.core.world import DomainComponent


@dataclass(frozen=True, slots=True)
class LocationIntent(DomainComponent):
    city: str
    country_code: str | None = None
```

> The `status` field the scaffold generates is the state field the agent's state machine advances (`--state-field`). Rename it or drop it — the FSM reads the field you name.

### 2. Events (Domain Language)
Events are the absolute source of truth.

```bash
uv run knt new event weather.WeatherResolved
```
*Open `src/weather_platform/contexts/weather/events/weather_resolved.py` and modify the payload:*
```python
from typing import TypedDict
from kntgraph.core.event import Event, correlation_middleware


class WeatherResolvedPayload(TypedDict):
    city: str
    temperature_celsius: float
    condition: str


def weather_resolved(
    agent_id: str, payload: WeatherResolvedPayload, causation_id: str
) -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type="weather.resolved",
        data=payload,
        causation_id=causation_id,
        correlation=correlation_middleware.current(),
    )
```

### 3. Tools (I/O Side-Effects)
Tools are where impure operations (network requests, databases) happen. We will use `httpx` to hit the Open-Meteo API.

```bash
uv run knt new tool weather.OpenMeteoApi
```
*Modify `src/weather_platform/contexts/weather/tools/open_meteo_api.py`:*
```python
import httpx
from typing import Any
from kntgraph.core.result import Result, Ok, Err, ToolError
from kntgraph.tools.worker import tool_worker


@tool_worker(name="open_meteo_api", description="Fetches weather for coordinates.")
class OpenMeteoApi:
    async def invoke(
        self, latitude: float, longitude: float, *, idempotency_key: str
    ) -> Result[dict[str, Any], ToolError]:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current_weather=true"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                return Ok(data["current_weather"])
        except Exception as e:
            return Err(ToolError(
                code="upstream_unavailable",
                message="The weather provider is unreachable.",
                retryable=True,
                cause=e,
            ))
```

### 4. Systems (Pure Logic)
Systems read the state and emit new Events. They are 100% pure and fast.

```bash
uv run knt new system weather.WeatherRouter
```

The CLI generated a boilerplate function in `systems/weather_router_system.py`. 

To illustrate how Kinetgraph handles execution paths, this system will support **Path B (System-Mediated Flow)** by responding to a high-level intent (`weather.get_weather` or gateway role request `tool.weather_agent.requested`) to request the weather tool, and then reacting to its completion to emit the resolved weather event. 

*(Note: **Path A - Direct Tool Invocation** is when the client bypasses this system and calls the tool directly via the API gateway using `type: "tool.invoke"`).*

Open `src/weather_platform/contexts/weather/systems/weather_router_system.py` and implement the two-step reactive flow:

```python
from kntgraph.core.world import World
from kntgraph.core.event import Event, CorrelationContext, correlation_middleware
from kntgraph.tools.system import ToolAwareSystem
from ..events.weather_resolved import weather_resolved


def weather_router_system(world: World) -> list[Event]:
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
```

### 5. The Two Execution Cycles (Concordos)

An agent runs two cycles. The **reactive** cycle is a state machine that guards the transitions without spending a token; the **deliberative** cycle is a workflow that dispatches the tools and compensates when a step fails.

```bash
uv run knt concordo add fsm weather.WeatherFSM --component LocationIntent --state-field status
uv run knt concordo new saga weather.WeatherSaga --steps fetch:open_meteo_api,resolve:resolve_weather
```

The commands take the **bounded context** as part of the target, exactly like `knt new <artifact>` does. The files land in `src/weather_platform/contexts/weather/concordos/`, and the state machine imports its component from the context:

```python
# src/weather_platform/contexts/weather/concordos/weather_fsm_concordo.py
from kntgraph.concordos.fsm import BusinessFSMConcordo, FSMConfig, FSMTransition

from ..components.location_intent import LocationIntent


WeatherFSMConcordo = BusinessFSMConcordo(FSMConfig(
    component_type=LocationIntent,
    state_field="status",
    transitions={
        "draft": {
            "weather.requested": FSMTransition(to="fetching"),
        },
    },
    on_entry={},
    terminal=frozenset(),
))
```

Fill in the transitions and the saga steps with your own rules. A guard is a `Specification` — a pure predicate over the carrier — and the same rule works as a state-machine guard and as a saga skip condition.

### 6. The Agent Declaration (L2 Security & Orchestration)

The agent is **declared as data**. One frozen dataclass names the persona, the capability policy, the state machine and the workflows; the framework derives the installable runtime from it.

```bash
uv run knt new agent weather.WeatherAgent
```

The CLI derives the declaration from what the context already holds — the tool allow-list is the context's tools, the domain types are its components, and the two cycles are its concordos:

```python
# src/weather_platform/contexts/weather/agents/weather_agent.py
from kntgraph.agents import AgentDefinition, RoleSpec

from ..components.location_intent import LocationIntent
from ..concordos.weather_fsm_concordo import WeatherFSMConcordo
from ..concordos.weather_saga_concordo import WeatherSagaConcordo


WEATHER_AGENT = AgentDefinition(
    name="weather",
    persona=RoleSpec(
        persona="weather",
        allowed_tools=("open_meteo_api",),
    ),
    role_event_type="role.assigned",
    fsm=WeatherFSMConcordo,
    sagas=(WeatherSagaConcordo,),
    domain_types=(LocationIntent,),
)
```

> The `allowed_tools` list is the agent's **capability policy** (ADR-088 §3.5). It is projected into `RoleComponent.allowed_tools`, which is what gate 2 of the three-gate model reads. An empty allow-list forbids every tool — the role is installed, so the permissive fallback does not apply.

---

## Phase 4: Wiring and Execution

The CLI has already generated the scaffolding — and it keeps it in sync. Every time you add a tool, a concordo or an agent to the context, the CLI regenerates the derived files (the declaration and the dispatcher) **as long as you have not edited them by hand**. A file you edited is left alone, and the CLI says so.

1. Open `src/weather_platform/contexts/weather/dispatcher.py`. It registers the tools and installs the declaration:

```python
from .tools.open_meteo_api import OpenMeteoApi
from .agents.weather_agent import WEATHER_AGENT

# Inside build_weather_dispatcher:
worker_manager.register(OpenMeteoApi(), acl=default_acl())

# ...after the dispatcher is created:
WEATHER_AGENT.install(
    dispatcher,
    principal=Principal.from_agent_id(
        "weather.weather",
        level=PrincipalLevel.agent,
        key_id="local-dev",
    ),
)
```

> The `Principal` is a runtime credential; it is never stored in a declaration. The scaffold derives the tenant from the context name — a deployment that resolves the tenant per request builds its own.

2. Open `src/weather_platform/main.py`. The CLI generated an HTTP Gateway ready to run. Just uncomment the Context references!

```python
from src.weather_platform.contexts.weather.dispatcher import build_weather_dispatcher

# Inside build_monolith():
weather_dispatcher = build_weather_dispatcher(log)

# Inside lifespan():
weather_task = asyncio.create_task(weather_dispatcher.start())
```

### Running the Platform

Ensure Redis is running locally on port 6379, then start your FastAPI monolith:

```bash
python src/weather_platform/main.py
```

Send a request via the HTTP Gateway using one of the two execution paths:

#### Path A: Direct Tool Invocation (Bypass Systems)
Trigger the tool directly from the client:

```bash
curl -X POST "http://localhost:8000/agents/user-123/intents" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: demo-key" \
     -d '{
           "type": "tool.invoke",
           "tool": "open_meteo_api",
           "args": {"latitude": 51.5, "longitude": -0.12}
         }'
```

**What happens:** The HTTP Gateway ingests the intent, detects `type: "tool.invoke"`, and queues the tool request directly to the background `WorkerManager` via the `ToolRouter`. The worker invokes `OpenMeteoApi` directly and publishes the completion.

#### Path B: System-Mediated Flow (Reactive Agent)
Trigger the high-level intent via the agent role:

```bash
curl -X POST "http://localhost:8000/agents/user-123/intents" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: demo-key" \
     -d '{
           "type": "role.invoke",
           "role": "weather_agent",
           "args": {
             "city": "London",
             "latitude": 51.5,
             "longitude": -0.12
           }
         }'
```

**What happens:** The gateway emits a `tool.weather_agent.requested` event containing the city and coordinates. The reactive dispatcher folds this event. `weather_router_system` detects the phase, starts/propagates the correlation context, and emits `tool.open_meteo_api.requested`. Once the worker manager completes the tool call, `weather_router_system` reads the result and emits the final `weather.weather_resolved` event to the stream.

You have successfully used the `knt` CLI to build a scalable, production-ready Modular Monolith supporting both execution paths!

---

## Command Reference

| Command | Description |
|---|---|
| `knt init project <name>` | Bootstraps a new repository (`--use-intent-http` for FastAPI). |
| `knt new context <name>` | Creates a Bounded Context structure and its `dispatcher.py`. |
| `knt new system <context>.<name>` | Scaffolds a pure `WorldSystem`. |
| `knt new component <context>.<name>` | Scaffolds a `DomainComponent` subclass. |
| `knt new event <context>.<name>` | Scaffolds an Event Factory with `TypedDict`. |
| `knt new tool <context>.<name>` | Scaffolds an Impure Tool class (`@tool_worker`). |
| `knt new agent <context>.<name>` | Scaffolds the agent declaration (`AgentDefinition` + `RoleSpec`). |
| `knt concordo add fsm <context>.<name>` | Scaffolds a `BusinessFSMConcordo` for an existing component. |
| `knt concordo new saga <context>.<name>` | Scaffolds a `WorkflowSagaConcordo` from `--steps a:tool,b:tool`. |
| `knt upgrade check` | Reports the generated files that drifted from the current templates. |
| `knt upgrade apply <path>` | Regenerates one generated file (`--force` overwrites local edits). |
| `knt upgrade apply-all` | Regenerates every generated file. |
| `knt keys generate` | Generates Ed25519 L1 Security Keys for an agent. |

---

## The Derived Files

Two of the generated files are **derived** from the context: the agent declaration and the dispatcher. The CLI reads the context's artifacts — the tools, the components, the concordos and the other agents — and renders them.

| File | Derived from |
|---|---|
| `contexts/<ctx>/agents/<name>_agent.py` | The context's tools, components and concordos. |
| `contexts/<ctx>/dispatcher.py` | The context's tools and agent declarations. |

Every `knt new` / `knt concordo` command regenerates them **when they are still the template's untouched output**. A file you edited by hand is never overwritten: the CLI prints a hint and points at `knt upgrade apply <path> --force`.

The scanner reads the **AST** of each artifact, not its filename — a renamed class or a renamed tool is followed correctly.
