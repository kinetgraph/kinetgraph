# Kinetgraph CLI Guide: Building a Weather Platform

The `knt` CLI is the official scaffolding tool for Kinetgraph. It strictly enforces Domain-Driven Design (DDD) and Kinetgraph's architectural standards (ADRs) by generating correctly structured boilerplate code.

In this guide, we will learn how to use the CLI by building a **Weather Platform**. We will scaffold a new project, create a bounded context, generate pure Systems and impure Tools, and connect them all via a Reactive Dispatcher.

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
uv run knt init weather_platform --use-intent-http
```

### What did this do?
- **`--use-intent-http`**: This flag tells the CLI to scaffold a `main.py` configured with FastAPI, `ToolRouter`, and `WorkerManager`. It wires up an HTTP Gateway that can receive intents from users.
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
It generated `src/weather_platform/contexts/weather/dispatcher.py` and empty subdirectories for `agents`, `components`, `events`, `systems`, and `tools`.
The `dispatcher.py` file is the "brain" of this context. It aggregates all pure systems and impure tools into a single `ReactiveDispatcher`.

---

## Phase 3: Building the Domain (DDD & ECS)

Now we will use the `knt new` group to generate the building blocks of our Weather Agent.

### 1. Components (State)
Components represent the immutable state in the ECS (Entity-Component-System) architecture. 

```bash
uv run knt new component weather.LocationIntent
```
*Open `src/weather_platform/contexts/weather/components/location_intent.py` and modify it:*
```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class LocationIntent:
    city: str
    country_code: str | None = None
```

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

def weather_resolved(agent_id: str, payload: WeatherResolvedPayload, causation_id: str) -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type="weather.resolved",
        data=payload,
        causation_id=causation_id,
        correlation=correlation_middleware.current()
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
from kntgraph.core.result import Result, Ok, Err
from kntgraph.agents.tools.protocol import tool_worker

@tool_worker(name="open_meteo_api", description="Fetches weather for coordinates.")
class OpenMeteoApi:
    async def invoke(self, latitude: float, longitude: float, *, idempotency_key: str) -> Result[dict[str, Any], Exception]:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current_weather=true"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                return Ok(data["current_weather"])
        except Exception as e:
            return Err(e)
```

### 4. Systems (Pure Logic)
Systems read the state and emit new Events. They are 100% pure and fast.

```bash
uv run knt new system weather.WeatherRouter
```
*The CLI generated a boilerplate AsyncIterator function in `systems/weather_router.py`. Here is how you would implement it to request the `open_meteo_api` tool when a user intents to check the weather.*

### 5. Agents (L2 Security & Orchestration)
The Agent definition binds specific Systems and Tools together under a Security Capability Policy.

```bash
uv run knt new agent weather.WeatherAgent
```
*Modify `src/weather_platform/contexts/weather/agents/weather_agent.py`:*
```python
from ..systems.weather_router import weather_router
from ..tools.open_meteo_api import OpenMeteoApi
from kntgraph.security.authorization import CapabilityPolicy

def build_weather_agent_policy() -> CapabilityPolicy:
    return CapabilityPolicy(
        allowed_events=["weather.*", "tool.*"],
        denied_events=[],
    )

def get_weather_agent_systems() -> list:
    return [weather_router]

def get_weather_agent_tools() -> list:
    return [OpenMeteoApi()]
```

---

## Phase 4: Wiring and Execution

The CLI has already generated the scaffolding. Let's wire it up.

1. Open `src/weather_platform/contexts/weather/dispatcher.py` and import the lists from your Agent:
```python
from .agents.weather_agent import get_weather_agent_systems, get_weather_agent_tools

# Inside build_weather_dispatcher:
tools = get_weather_agent_tools()
systems = get_weather_agent_systems()
```

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

Send a request via the HTTP Gateway!

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

**Success!** The HTTP Gateway ingested your intent, the `ReactiveDispatcher` queued the tool request directly to the background `WorkerManager` via the `ToolRouter`. The worker hit the Open-Meteo API using `httpx`, and the result was written back to the Event Log. 

You have successfully used the `knt` CLI to build a scalable, production-ready Modular Monolith!

---

## Command Reference

| Command | Description |
|---|---|
| `knt init <name>` | Bootstraps a new repository (`--use-intent-http` for FastAPI). |
| `knt new context <name>` | Creates a Bounded Context structure and its `dispatcher.py`. |
| `knt new system <context>.<name>` | Scaffolds a pure `WorldSystem`. |
| `knt new component <context>.<name>` | Scaffolds a pure ECS Dataclass. |
| `knt new event <context>.<name>` | Scaffolds an Event Factory with `TypedDict`. |
| `knt new tool <context>.<name>` | Scaffolds an Impure Tool class (`@tool_worker`). |
| `knt new agent <context>.<name>` | Scaffolds a `CapabilityPolicy` and exports systems/tools. |
| `knt keys generate` | Generates Ed25519 L1 Security Keys for an agent. |
