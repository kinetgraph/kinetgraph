# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event.operational -- The closed set of operational event types.

The framework owns the namespace of OPERATIONAL events.
Application code MUST use `Event.operation_from(...)` to
create them. This guarantees that:

  - the event_type spelling is consistent
    (e.g. ``"agent.spawned"``);
  - event_class is always ``"lifecycle"``;
  - the operational projection in ``world.py`` can map
    them to ``OperationalPhase`` values without guesswork.

DOMAIN event types are application-defined and are
constructed via ``Event.domain_from(...)``. The framework
does NOT enumerate them.

The `OPERATIONAL_EVENT_TO_PHASE` mapping is the single
source of truth for the wire-level event_type → the
``OperationalPhase`` value used by ``core.lifecycle``. Kept
here (not in lifecycle.py) because the source of truth is
the event_type string, not the phase enum.
"""

from __future__ import annotations

from enum import Enum


class OperationalEventType(str, Enum):
    """
    The closed set of operational event types the framework emits.

    Naming convention: ``agent.<verb>`` in past tense, lower-snake-case.
    """

    SPAWNED = "agent.spawned"
    IDLE = "agent.idle"
    RUNNING = "agent.running"
    BLOCKED = "agent.blocked"
    CHECKPOINTED = "agent.checkpointed"
    TERMINATED = "agent.terminated"


# Mapping from operational event_type to the OperationalPhase value
# declared in core.lifecycle. Kept here (not in lifecycle.py) because
# the source of truth is the event_type string, not the phase enum.
OPERATIONAL_EVENT_TO_PHASE: dict[str, str] = {
    OperationalEventType.SPAWNED.value: "spawned",
    OperationalEventType.IDLE.value: "idle",
    OperationalEventType.RUNNING.value: "running",
    OperationalEventType.BLOCKED.value: "blocked",
    OperationalEventType.CHECKPOINTED.value: "checkpointed",
    OperationalEventType.TERMINATED.value: "terminated",
}


__all__ = [
    "OPERATIONAL_EVENT_TO_PHASE",
    "OperationalEventType",
]
