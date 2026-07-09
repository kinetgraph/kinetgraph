from typing import Any
from kntgraph.runner import ReactiveDispatcher
from kntgraph.stream.event_log import EventLog
from kntgraph.tools.router import ToolRouter

from .agents.weather_agent import get_weather_agent_systems, get_weather_agent_tools

def build_weather_dispatcher(log: EventLog, redis: Any, tool_router: ToolRouter) -> ReactiveDispatcher:
    systems = get_weather_agent_systems()
    
    dispatcher = ReactiveDispatcher(
        log=log,
        systems=systems,
        redis=redis,
        tool_router=tool_router,
        poll_interval=0.5
    )
    
    return dispatcher

def get_weather_tools() -> list:
    return get_weather_agent_tools()