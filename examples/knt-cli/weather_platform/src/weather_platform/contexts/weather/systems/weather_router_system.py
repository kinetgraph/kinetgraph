from kntgraph.core.world import World
from kntgraph.core.event import Event


def weather_router_system(world: World) -> list[Event]:
    events = []
    # In Kinetgraph, systems read Intents and emit tool requests
    for agent_id, agent_state in world.views.items():
        # Check if the tool has completed
        # For simplicity, we assume we check components or something
        pass

    return events
