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
    """

    def wrapper(cls: T) -> T:
        DomainComponent.__domain_registry__[event_type] = cls
        return cls

    return wrapper


__all__ = ["DomainComponent", "domain_component"]
