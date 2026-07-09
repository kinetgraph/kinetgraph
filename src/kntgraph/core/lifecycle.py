# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Lifecycle — the two-phase model of an agent.

FMH v2.0 splits agent lifecycle into two orthogonal axes:

  1. Operational lifecycle  — does the agent exist in the runtime
     and in what mode is it?  This is the concern of the
     RUNTIME / FRAMEWORK, not the application.

  2. Domain lifecycle       — what step of the business process is
     the agent in?  This is the concern of the APPLICATION.

Both are encoded as event_class="lifecycle" and event_class="domain"
events on the same Redis Stream. The current phase of an agent on
each axis is derived from the most recent event of that class.

The framework provides the OPERATIONAL vocabulary (spawned, idle,
running, ...). The APPLICATION provides the DOMAIN vocabulary —
DomainPhase is typed as `str` on purpose so that applications can
declare their own set of legal domain states per aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


# Operational phases — defined by the framework, immutable across
# applications.
OperationalPhase = Literal[
    "spawned",  # agent was just created
    "idle",  # exists, awaiting work
    "running",  # a system is processing events for this agent
    "blocked",  # waiting on an external dependency
    "checkpointed",  # paused (long-running task with a checkpoint)
    "terminated",  # retired; no more events accepted
]


# Terminal operational phases (no further transitions allowed).
TERMINAL_OPERATIONAL: frozenset[OperationalPhase] = frozenset({"terminated"})


def is_terminal_operational(phase: OperationalPhase) -> bool:
    return phase in TERMINAL_OPERATIONAL


@dataclass(frozen=True, slots=True)
class DomainPhase:
    """
    A point on the domain lifecycle of an agent.

    `phase` is a free-form string. The application defines the legal
    transitions (e.g. for a nota fiscal: "received", "validated",
    "lancada", "transmitida", "paga"). The framework does not
    enforce them.

    `reason` is an optional human-readable hint (e.g. "rejected for
    missing CNPJ"). `updated_at` is the event timestamp.
    """

    phase: str
    updated_at: datetime
    reason: Optional[str] = None

    def __str__(self) -> str:
        return self.phase
