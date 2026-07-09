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

from .archetype import ArchetypeId, archetype_of
from .agent_id import (
    AGENT_ID_RE,
    MAX_AGENT_ID_LEN,
    assert_valid_agent_id,
    validate_agent_id,
)
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
    DomainPhase,
    OperationalPhase,
    TERMINAL_OPERATIONAL,
    is_terminal_operational,
)
from .world import (
    WorldQuery,
)
from .result import (
    Err,
    Ok,
    Result,
    Success,
    Failure,
    UnwrapError,
    RailwayError,
    ValidationError,
    PersistenceError,
    BusinessError,
    ToolError,
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
from .world import AgentView, Projection, World, project_default

__all__ = [
    # archetype
    "ArchetypeId",
    "archetype_of",
    # agent_id
    "AGENT_ID_RE",
    "MAX_AGENT_ID_LEN",
    "assert_valid_agent_id",
    "validate_agent_id",
    # component
    "ComponentInstance",
    "ComponentMeta",
    "component_meta",
    # event
    "OPERATIONAL_EVENT_TO_PHASE",
    "CorrelationContext",
    "CorrelationMiddleware",
    "CorrelationScope",
    "Event",
    "EventClass",
    "OperationalEventType",
    "correlation_middleware",
    "generate_deterministic_event_id",
    # lifecycle
    "DomainPhase",
    "OperationalPhase",
    "TERMINAL_OPERATIONAL",
    "is_terminal_operational",
    # query
    "WorldQuery",
    # result
    "Err",
    "Ok",
    "Result",
    "Success",
    "Failure",
    "UnwrapError",
    "RailwayError",
    "ValidationError",
    "PersistenceError",
    "BusinessError",
    "ToolError",
    # storage
    "ArchetypeStorage",
    # system
    "Cyclic",
    "CyclicSystem",
    "Reactive",
    "ReactiveSystem",
    "System",
    "WorldSystem",
    # world
    "AgentView",
    "Projection",
    "World",
    "WorldQuery",
    "project_default",
]
