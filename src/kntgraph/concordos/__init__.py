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

from typing import TYPE_CHECKING, Protocol, runtime_checkable

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

if TYPE_CHECKING:
    from kntgraph.runner.reactive import ReactiveDispatcher

__all__ = [
    "AndSpec",
    "Composable",
    "Concordo",
    "ConcordoCatalog",
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


@runtime_checkable
class Concordo(Protocol):
    """
    The public surface every Concordo exposes.

    ``name``    -- stable identifier (``fsm:Invoice``,
                   ``saga:nfe_emission``). Used by the
                   framework's ``ConcordoCatalog`` and by
                   log/metrics tagging.
    ``version`` -- semver string. Bumping it is the
                   recommended migration signal when a
                   Concordo's emitted event schema or
                   state semantics change.
    ``install`` -- idempotent registration against the
                   ``ReactiveDispatcher``. Each Concordo
                   registers one or more ``WorldSystem``s
                   (the post-ADR-018 shape) via
                   ``dispatcher.add_system(...)``.
                   ``ConcordoCatalog.install_all``
                   de-duplicates by name.
    """

    name: str
    version: str

    def install(self, dispatcher: "ReactiveDispatcher") -> None: ...


class ConcordoCatalog:
    """
    Bag of ``Concordo`` instances with idempotent installation.

    ``install_all`` iterates the catalog and calls
    ``concordo.install(dispatcher)`` once per unique ``name``.
    A second pass with the same name is a no-op (first one
    wins). This keeps ``app_runner.py`` free of dedup logic
    and makes double-imports of the same vertical module safe.
    """

    def __init__(self, *concordos: Concordo) -> None:
        self._concordos: dict[str, Concordo] = {}
        for c in concordos:
            if c.name in self._concordos:
                # Idempotent; first one wins. A vertical that
                # genuinely needs two different versions of the
                # same Concordo must give them different names.
                continue
            self._concordos[c.name] = c

    def install_all(self, dispatcher: "ReactiveDispatcher") -> None:
        for concordo in self._concordos.values():
            concordo.install(dispatcher)
