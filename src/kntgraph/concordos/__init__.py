# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos -- composable behavioral patterns (ADR-069).

A *Concordo* is a named, composable behavioral pattern that
wires existing framework modules into a coherent end-to-end
behavior. This package provides the shared condition language
(the Specification Pattern) and the built-in Specifications
that both Concordos (BusinessFSM, WorkflowSaga) use.

Public surface:

  - ``base.Specification`` / ``StepContext`` /
    ``Composable`` / ``AndSpec`` / ``OrSpec`` / ``NotSpec``
    / ``ViewTrigger`` -- the condition language (ADR-069 §2).
  - ``specs.*`` -- the built-in Specifications (ADR-069 §2.3).

The ``Concordo`` Protocol and ``ConcordoCatalog`` (ADR-069
§1.3.1, §3.2, §4) are the composition mechanism: a Concordo
is a frozen bundle of ``(name, systems, projections)``;
the catalog iterates the bundle and registers against the
dispatcher via ``dispatcher.add_system(...)`` /
``dispatcher.add_projection(...)`` -- the same API the
application uses for any custom system (ADR-069 §11.11).
"""

from pathlib import Path
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
from ._loader import (
    ConcordoBundleError,
    LoadedBundle,
    load_bundle_dict,
    load_bundle_json,
    load_bundle_yaml,
)

if TYPE_CHECKING:
    from kntgraph.concordos._loader import LoadedBundle
    from kntgraph.core.system import WorldSystem
    from kntgraph.runner.reactive import ReactiveDispatcher
    from kntgraph.runner.reactive_extensions import WorldProjection


@runtime_checkable
class Concordo(Protocol):
    """
    The public surface every Concordo exposes (ADR-069 §3.2).

    A Concordo is a frozen bundle of ``(name, systems,
    projections)``. The Protocol is structural; concrete
    bundles (``BusinessFSMConcordo``, ``WorkflowSagaConcordo``)
    are frozen dataclasses that satisfy it without explicit
    inheritance.

    ``name`` -- stable identifier
                (``fsm:KnowledgeLifecycle``,
                ``saga:EntityExtractionSaga``).
    ``systems`` -- tuple of ``WorldSystem`` instances the
                   dispatcher should register.
    ``projections`` -- tuple of ``WorldProjection`` instances
                       the dispatcher should register.

    The catalog (§4) iterates each bundle and calls the
    dispatcher's registration API. The application can
    also iterate ``concordo.systems`` directly when it
    needs explicit ordering.

    Side effects (DLQ ingestion, metrics, notifications)
    are wired by the application via
    ``dispatcher.subscribe`` (§5), not by the Concordo --
    Concordos stay pure and side-effect-free.
    """

    name: str
    systems: tuple["WorldSystem", ...]
    projections: tuple["WorldProjection", ...]


class ConcordoCatalog:
    """
    Bag of ``Concordo`` instances with idempotent registration.

    ``install_all`` iterates each Concordo's ``systems`` and
    ``projections`` and calls
    ``dispatcher.add_system(...)`` /
    ``dispatcher.add_projection(...)`` -- the same
    registration API the framework exposes for any custom
    system or projection. A duplicate ``name`` is a no-op
    (the catalog dedupes; first one wins).

    The catalog is sugar, not a required composition root:
    the application can iterate ``concordo.systems`` directly
    when it needs explicit ordering.
    """

    def __init__(self, *concordos: Concordo) -> None:
        self._concordos: dict[str, Concordo] = {}
        for c in concordos:
            if c.name in self._concordos:
                # Idempotent; first one wins. A vertical that
                # genuinely needs two different Concordos with
                # the same name must give them different names.
                continue
            self._concordos[c.name] = c

    def install_all(self, dispatcher: "ReactiveDispatcher") -> None:
        for concordo in self._concordos.values():
            for system in concordo.systems:
                dispatcher.add_system(system)
            for projection in concordo.projections:
                dispatcher.add_projection(projection)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ConcordoCatalog":
        """Load a catalog from a YAML bundle file.

        Wraps :func:`load_bundle_yaml`. Each declared FSM
        and saga is wrapped in its corresponding ``Concordo``.
        """
        from ._loader import load_bundle_yaml

        loaded = load_bundle_yaml(path)
        return cls(*_bundle_to_concordos(loaded))

    @classmethod
    def from_json(cls, path: str | Path) -> "ConcordoCatalog":
        """Load a catalog from a JSON bundle file.

        Wraps :func:`load_bundle_json``.
        """
        from ._loader import load_bundle_json

        loaded = load_bundle_json(path)
        return cls(*_bundle_to_concordos(loaded))

    @classmethod
    def from_dict(cls, d: dict) -> "ConcordoCatalog":
        """Load a catalog from a dict.

        Wraps :func:`load_bundle_dict``.
        """
        from ._loader import load_bundle_dict

        loaded = load_bundle_dict(d)
        return cls(*_bundle_to_concordos(loaded))


def _bundle_to_concordos(loaded) -> tuple[Concordo, ...]:
    """Wrap a :class:`LoadedBundle`'s runtime objects into
    ``Concordo`` instances.

    Currently guards (mini-language expressions in the
    YAML) are not wired into ``FSMTransition.guard``;
    that's a follow-up integration with the mini-language
    ``SpecRegistry``. See ADR-073 §4.6.
    """
    from .fsm import BusinessFSMConcordo
    from .saga import WorkflowSagaConcordo

    concordos: list[Concordo] = []
    if loaded.fsm is not None:
        concordos.append(BusinessFSMConcordo(loaded.fsm))
    for saga in loaded.sagas:
        concordos.append(WorkflowSagaConcordo(saga))
    return tuple(concordos)
