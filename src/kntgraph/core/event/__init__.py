# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Event — the only source of state change in FMH.

Design principles (FMH v2.0):

  1. Events are immutable, append-only.
  2. The Redis Stream is the source of truth; the World is a fold over
     events; an agent's components are derived from its event history.
  3. There are TWO event classes, distinguished by `event_class`:

       - "lifecycle"  → operational state of the agent
                        (spawned, idle, running, blocked, terminated, ...)
       - "domain"     → business state of the agent
                        (validated, paid, transmitted, ...)

     Both travel on the same Redis Stream. Systems filter by event_class
     to react to the dimension they care about. This is a convention, not
     a separate storage layer.

  4. The CURRENT operational phase of an agent = the `event_class`
     of the last "lifecycle" event for that agent.

     The CURRENT domain phase of an agent = the last "domain" event
     for that agent (or a fold of recent domain events, depending on
     the projection function chosen by the application).

  5. The combination (event_type + agent_id + causation_id + payload)
     is deterministic: an event_id is uuid5 of those fields, which gives
     natural idempotency when the same event is re-emitted by replay.

  6. `correlation_id` ties together a logical flow. `causation_id`
     points to the immediate parent event. Together they form a DAG
     per flow, suitable for audit trails.

This module is a thin facade. The implementation is split across
the `event` subpackage:

  - `event.constants`    — `EventClass` Literal + `ALLOWED_EVENT_CLASSES`
    + the UUID namespaces reserved for deterministic ids.
  - `event.operational`  — `OperationalEventType` enum +
    `OPERATIONAL_EVENT_TO_PHASE` mapping.
  - `event.correlation`  — `CorrelationContext` +
    `CorrelationMiddleware` + `CorrelationScope` +
    `correlation_middleware` (the singleton).
  - `event.validators`   — runtime guards (`validate_event_type`,
    `validate_data`, `utcnow`).
  - `event.id_helpers`   — `generate_deterministic_event_id`.
  - `event.event`        — the `Event` dataclass + the 3 builders
    (`create`, `operation_from`, `domain_from`) + the wire-method
    thin wrappers (`to_dict`, `from_dict`, `to_json`,
    `from_json`) that delegate to the codec.
  - `event.codec`        — `event_to_dict`, `event_from_dict`,
    `event_to_json`, `event_from_json` (the canonical wire
    format; the methods on `Event` delegate here).
"""

from .constants import ALLOWED_EVENT_CLASSES, EventClass
from .correlation import (
    CorrelationContext,
    CorrelationMiddleware,
    CorrelationScope,
    correlation_middleware,
)
from .event import Event
from .id_helpers import generate_deterministic_event_id
from .operational import OPERATIONAL_EVENT_TO_PHASE, OperationalEventType


__all__ = [
    "ALLOWED_EVENT_CLASSES",
    "CorrelationContext",
    "CorrelationMiddleware",
    "CorrelationScope",
    "Event",
    "EventClass",
    "OPERATIONAL_EVENT_TO_PHASE",
    "OperationalEventType",
    "correlation_middleware",
    "generate_deterministic_event_id",
]
