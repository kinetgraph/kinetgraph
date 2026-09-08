# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.specs -- built-in Specifications (ADR-069 §2.3).

The framework ships a standard library of Specifications
that cover the most common conditions. Each inherits
``Specification`` (which provides the ``Composable``
mixin's ``and_/or_/not_``); concrete classes only declare
their data fields and implement ``is_satisfied_by``.

Business verticals define their own Specifications using
the same protocol (see ADR-069 §2.4). Vertical
Specifications must be zero-argument instantiable unless
they genuinely need parameters; configuration that varies
per evaluation belongs in the ``StepContext`` (e.g.
``now``), not in the Specification.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import Specification, StepContext

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue

__all__ = [
    "ContinuityToolUsed",
    "DomainStateIs",
    "ProfileTierIs",
    "StepCompleted",
    "StepFailed",
    "StepResultEquals",
    "StepTimedOut",
]


@dataclass(frozen=True, slots=True)
class StepCompleted(Specification):
    """True when the named step completed successfully."""

    step_name: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return ctx.step_states.get(self.step_name) == "completed"


@dataclass(frozen=True, slots=True)
class StepFailed(Specification):
    """True when the named step failed (any reason)."""

    step_name: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return ctx.step_states.get(self.step_name) in ("failed", "timed_out")


@dataclass(frozen=True, slots=True)
class StepTimedOut(Specification):
    """True when the named step timed out (ADR-045)."""

    step_name: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        return ctx.step_states.get(self.step_name) == "timed_out"


@dataclass(frozen=True, slots=True)
class StepResultEquals(Specification):
    """
    True when a field in a step result equals a value.

    ``value`` is ``JsonValue`` (not ``object``): the
    framework's type-discipline skill forbids bare
    ``object`` in framework code. Tool workers return
    ``Mapping[str, JsonValue]`` payloads; comparing against
    an arbitrary Python object would silently always be
    False and mask bugs.
    """

    step_name: str
    field: str
    value: "JsonValue"

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        result = ctx.step_results.get(self.step_name)
        if not isinstance(result, Mapping):
            return False
        return result.get(self.field) == self.value


@dataclass(frozen=True, slots=True)
class DomainStateIs(Specification):
    """True when the DomainComponent has a specific state value."""

    field: str
    value: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if ctx.domain is None:
            return False
        return getattr(ctx.domain, self.field, None) == self.value


@dataclass(frozen=True, slots=True)
class ProfileTierIs(Specification):
    """True when ProfileComponent.tier matches."""

    tier: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if ctx.profile is None:
            return False
        return ctx.profile.tier == self.tier


@dataclass(frozen=True, slots=True)
class ContinuityToolUsed(Specification):
    """
    True when the named tool appears in
    ``ContinuityComponent.last_tools`` (i.e. it was the last
    tool invoked in the recent window).

    The component (ADR-042 §2.3) carries
    ``last_tools: dict[str, str]`` where the value is the
    tool's last invocation timestamp, not a usage count.
    Quota enforcement is a different concern (it lives on
    ``ProfileComponent`` or a dedicated QuotaComponent) and
    is out of scope for ADR-069.
    """

    tool_name: str

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        if ctx.continuity is None:
            return False
        return self.tool_name in ctx.continuity.last_tools
