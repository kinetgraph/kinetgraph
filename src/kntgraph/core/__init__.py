# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
FMH Core — pure, functional, event-sourced ECS.

This package provides the building blocks for the framework:

  - Component  : an immutable value object with stable class identity.
  - Event      : the only source of state change. Append-only.
  - System     : a pure function (World[, Event]) -> list[Event].
  - World      : a fold of the event stream at a given tick.
  - Storage    : the in-memory working set behind a World.
  - Query      : archetype-keyed retrieval of agents.
  - Archetype  : the (module, qualname) canonical key.
  - Lifecycle  : operational + domain phases for an agent.
  - Result     : railway-pattern error wrapper.
"""

from .agent_id import (
    AGENT_ID_RE,
    MAX_AGENT_ID_LEN,
    assert_valid_agent_id,
    validate_agent_id,
)
from .archetype import ArchetypeId, archetype_of
from .component import (
    ComponentInstance,
    ComponentMeta,
    component_meta,
)
from .event import (
    OPERATIONAL_EVENT_TO_PHASE,
    CorrelationContext,
    CorrelationMiddleware,
    CorrelationScope,
    Event,
    EventClass,
    OperationalEventType,
    correlation_middleware,
    generate_deterministic_event_id,
)
from .lifecycle import (
    TERMINAL_OPERATIONAL,
    DomainPhase,
    OperationalPhase,
    is_terminal_operational,
)
from .result import (
    BusinessError,
    Err,
    Failure,
    Ok,
    PersistenceError,
    RailwayError,
    Result,
    Success,
    ToolError,
    UnwrapError,
    ValidationError,
)
from .storage import ArchetypeStorage
from .system import (
    Cyclic,
    CyclicSystem,
    Reactive,
    ReactiveSystem,
    System,
    WorldSystem,
)
from .world import (
    AgentView,
    Projection,
    World,
    WorldQuery,
    project_default,
)

__all__ = [
    # agent_id
    "AGENT_ID_RE",
    "MAX_AGENT_ID_LEN",
    # event
    "OPERATIONAL_EVENT_TO_PHASE",
    "TERMINAL_OPERATIONAL",
    # world
    "AgentView",
    # archetype
    "ArchetypeId",
    # storage
    "ArchetypeStorage",
    "BusinessError",
    # component
    "ComponentInstance",
    "ComponentMeta",
    "CorrelationContext",
    "CorrelationMiddleware",
    "CorrelationScope",
    # system
    "Cyclic",
    "CyclicSystem",
    # lifecycle
    "DomainPhase",
    # result
    "Err",
    "Event",
    "EventClass",
    "Failure",
    "Ok",
    "OperationalEventType",
    "OperationalPhase",
    "PersistenceError",
    "Projection",
    "RailwayError",
    "Reactive",
    "ReactiveSystem",
    "Result",
    "Success",
    "System",
    "ToolError",
    "UnwrapError",
    "ValidationError",
    "World",
    # query
    "WorldQuery",
    "WorldSystem",
    "archetype_of",
    "assert_valid_agent_id",
    "component_meta",
    "correlation_middleware",
    "generate_deterministic_event_id",
    "is_terminal_operational",
    "project_default",
    "validate_agent_id",
]
