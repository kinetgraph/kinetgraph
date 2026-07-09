from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class LocationIntent:
    """
    ECS Component: LocationIntent.
    """
    city: str
    latitude: float
    longitude: float