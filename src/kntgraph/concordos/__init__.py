# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos -- composable behavioral patterns (ADR-069).

A *Concordo* is a named, versioned, composable behavioral
pattern that wires existing framework modules into a
coherent end-to-end behavior. This package provides the
shared condition language (the Specification Pattern) and
the built-in Specifications that both Concordos
(BusinessFSM, WorkflowSaga) use.

Public surface:

  - ``base.Specification`` / ``StepContext`` /
    ``Composable`` / ``AndSpec`` / ``OrSpec`` / ``NotSpec``
    / ``ViewTrigger`` — the condition language (ADR-069 §2).
  - ``specs.*`` — the built-in Specifications (ADR-069 §2.3).

The ``Concordo`` Protocol and ``ConcordoCatalog`` (ADR-069
§1.3.1, §6.3) are added in the same PR as the FSM/Saga
Concordos.
"""

from .base import (
    AndSpec,
    Composable,
    NotSpec,
    OrSpec,
    Specification,
    StepContext,
    ViewTrigger,
)
from .specs import (
    ContinuityToolUsed,
    DomainStateIs,
    ProfileTierIs,
    StepCompleted,
    StepFailed,
    StepResultEquals,
    StepTimedOut,
)

__all__ = [
    "AndSpec",
    "Composable",
    "ContinuityToolUsed",
    "DomainStateIs",
    "NotSpec",
    "OrSpec",
    "ProfileTierIs",
    "Specification",
    "StepCompleted",
    "StepContext",
    "StepFailed",
    "StepResultEquals",
    "StepTimedOut",
    "ViewTrigger",
]
