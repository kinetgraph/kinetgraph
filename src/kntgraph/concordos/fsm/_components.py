# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.fsm._components -- BusinessFSM ECS component (ADR-069 §3.3).

The FSM does not introduce a new component for state —
state lives in the existing ``DomainComponent``. It
introduces one component for audit: ``FSMAuditComponent``,
the last transition record for an agent, materialised from
``fsm.transitioned`` events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

__all__ = ["FSMAuditComponent"]


@dataclass(frozen=True, slots=True)
class FSMAuditComponent:
    """
    Last transition record for this agent.

    Materialised from ``fsm.transitioned`` events.
    Read-only for external systems.

    ``last_processed_event_id`` is the cursor the FSM uses
    to detect transitions that ``view.domain_phase`` (a
    single slot) would have hidden when an agent produces
    more than one domain event in a tick (ADR-069 §11.16,
    §11.18.1). It is set to the trigger's ``event_id`` every
    time the FSM emits a ``fsm.transitioned``; on the next
    tick the FSM compares it against the EventLog to run the
    delta-scan.
    """

    from_state: str
    to_state: str
    trigger_event_type: str
    trigger_event_id: str
    transitioned_at: datetime
    guard_evaluated: bool
    last_processed_event_id: Optional[str] = None
