from typing import TypedDict
from kntgraph.core.event import Event
from kntgraph.core.correlation import correlation_middleware


class WeatherResolvedPayload(TypedDict):
    """
    Payload for the WeatherResolved event.
    """

    city: str
    temperature_celsius: float
    condition: str


def weather_resolved(
    agent_id: str, data: WeatherResolvedPayload, causation_id: str | None = None
) -> Event:
    """
    Creates a 'weather.weather_resolved' event.
    """
    # Use continue_from if causation_id is provided, otherwise current()
    # Or implement custom correlation logic here.
    return Event.domain_from(
        agent_id=agent_id,
        type="weather.weather_resolved",
        data=data,  # type: ignore
        causation_id=causation_id,
        correlation=correlation_middleware.current(),
    )
