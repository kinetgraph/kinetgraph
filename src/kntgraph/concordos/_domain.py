# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
#
"""
concordos._domain -- reading domain components from a Specification.

A domain component is the agent's own business state: the
FSM's state field, the facts a Saga step checks, the policy a
guard enforces. The carrier holds the agent's domain
components keyed by their concrete class, so a Specification
reads one with ``ctx.get_component(MyComponent)`` and gets
exactly that class or nothing.

Some Specifications cannot name the class. The built-in
``DomainStateIs`` is the canonical case: it names a field and
lets the carrier resolve which component carries it. The
resolution is deterministic -- zero or several matches leave
the condition unmet -- so the Specification never reads an
arbitrary component.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import Specification

if TYPE_CHECKING:
    from kntgraph.core.world.component import DomainComponent

    from ._context import StepContext

__all__ = ["DomainStateIs", "find_domain_component"]


def find_domain_component(
    ctx: StepContext,
    field: str,
) -> DomainComponent | None:
    """The one domain component of ``ctx`` that carries ``field``.

    This is the escape hatch for Specifications that do not know
    the concrete class of the component they read. The match is
    unambiguous by construction: when zero or more than one
    component carries the field, the result is ``None`` and the
    caller treats the condition as unmet.

    A Specification that knows its component's class should call
    ``ctx.get_component(MyComponent)`` instead; this function
    trades static typing for the ability to name a field alone.
    """
    matches = [
        component
        for component in ctx.domain_components.values()
        if hasattr(component, field)
    ]
    if len(matches) != 1:
        return None
    return matches[0]


@dataclass(frozen=True, slots=True)
class DomainStateIs(Specification):
    """True when a domain component has a specific state value.

    The field, not the component class, is what the
    Specification names. The carrier resolves it to the one
    component that carries it; zero or several matches leave
    the condition unmet. A Specification that knows its
    class reads it through ``ctx.get_component``.
    """

    field: str
    value: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        component = find_domain_component(ctx, self.field)
        if component is None:
            return False
        return getattr(component, self.field, None) == self.value
