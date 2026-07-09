from kntgraph.security.principal import AlwaysAllowPolicy, Policy
from kntgraph.stream.event_log import EventLog

from ..systems.weather_router_system import weather_router_system
from ..tools.open_meteo_api import OpenMeteoApi

def build_weather_agent_policy() -> Policy:
    return AlwaysAllowPolicy()

def get_weather_agent_systems() -> list:
    return [weather_router_system]

def get_weather_agent_tools() -> list:
    return [OpenMeteoApi()]