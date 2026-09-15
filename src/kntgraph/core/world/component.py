# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
world.component -- The `Component` protocol.

Marker protocol for ECS Components (Domain Memory).
Users should inherit or implement this protocol when
creating domain-specific facts that must be projected
into the World.

Example:
    @dataclass(frozen=True, slots=True)
    class CompanySizeProjection(Component):
        company_size: str
"""

from typing import Callable, ClassVar, TypeVar


class DomainComponent:
    """
    Base class for Domain Memory ECS Components.
    """

    __domain_registry__: ClassVar[dict[str, type["DomainComponent"]]] = {}


T = TypeVar("T", bound=type[DomainComponent])


def domain_component(event_type: str) -> Callable[[T], T]:
    """
    Decorator to register a DomainComponent for auto-hydration during World folds.
    Must be applied AFTER @dataclass if slots=True is used.

    Example:
        @domain_component("my.event.loaded")
        @dataclass(frozen=True, slots=True)
        class MyComp(DomainComponent):
            pass

    Collision detection
    -------------------

    The registry is process-wide (a single
    ``ClassVar[dict]`` on :class:`DomainComponent`). When two
    modules register a different class for the same
    ``event_type``, the second import would silently overwrite
    the first and the fold would hydrate the wrong class for
    every subsequent event of that type — a subtle and
    load-order-dependent bug that is invisible to the test
    that registered last.

    To prevent this, the decorator detects three cases:

      1. ``event_type`` is already registered with the SAME
         class → idempotent re-registration (no-op).
      2. ``event_type`` is already registered with a DIFFERENT
         class → ``ValueError`` at import time. The error
         message names both classes and points to the
         ``__domain_registry__`` so the operator can either
         namespace the event_type or remove one of the
         registrations.
      3. ``event_type`` is not yet registered → registers
         normally.

    Test cleanup
    ------------

    Tests that use a namespaced event_type do not need any
    cleanup. Tests that reuse a generic event_type (e.g.
    ``"order.submit"``) collide; the namespacing is a one-line
    fix per test class.

    For tests that intentionally want a clean registry (rare;
    the registry is meant to be process-wide), call
    :func:`reset_domain_registry`.
    """

    def wrapper(cls: T) -> T:
        existing = DomainComponent.__domain_registry__.get(event_type)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"Duplicate @domain_component registration for "
                f"event_type={event_type!r}: "
                f"{existing.__module__}.{existing.__name__} is already "
                f"registered; refusing to overwrite with "
                f"{cls.__module__}.{cls.__name__}. "
                f"Either namespace the event_type (e.g. "
                f"'{event_type}.{cls.__name__}'), remove one of the "
                f"decorator applications, or call "
                f"reset_domain_registry() before importing both "
                f"modules."
            )
        DomainComponent.__domain_registry__[event_type] = cls
        return cls

    return wrapper


def reset_domain_registry() -> None:
    """
    Clear the ``DomainComponent.__domain_registry__``.

    Test-only helper. Production code MUST NOT call this — the
    registry is meant to be process-wide so a domain component
    registered once at import time is reachable by every fold
    without re-registration. The few tests that need a clean
    registry (e.g. to assert the registry contract itself)
    should call this in their setup.
    """
    DomainComponent.__domain_registry__.clear()


__all__ = ["DomainComponent", "domain_component", "reset_domain_registry"]
