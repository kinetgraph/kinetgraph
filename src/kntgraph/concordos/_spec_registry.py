# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
.. warning::

    **DEV status -- not recommended for production use.**

    This module is the Python entry point for the
    mini-language parser in user code
    (specifically :class:`MiniLangSpec`, which is part of
    ``__all__``). The grammar, built-in functions, AST
    node shapes, and ``MiniLangSpec``'s contract may
    change without notice in subsequent releases.

    Prefer :class:`kntgraph.concordos.Specification` or the
    :class:`Composable` combinators (``AndSpec`` /
    ``OrSpec`` / ``NotSpec``) when a stable contract is
    required. The bundle loader uses this module
    internally to turn named predicates declared in
    YAML/JSON bundles into :class:`Specification`
    instances.

concordos._spec_registry -- Spec name resolution for bundles.

A bundle's ``specifications:`` block declares named
predicates in the mini-language. Guards in FSM
transitions and Saga conditions reference those names
(e.g., ``guard: "ConfidencePassed"``). The registry
turns those names into ``Specification`` instances the
runtime can evaluate.

Three classes:

  - :class:`SpecRegistry` -- holds a name → expression map
    for one bundle; resolves names and builds
    :class:`Specification` objects on demand.
  - :class:`MiniLangSpec` -- a :class:`Specification` wrapper
    around a parsed mini-language AST. Built automatically
    by :meth:`SpecRegistry.resolve`.
  - :func:`build_builtin_spec` -- factory that turns a
    built-in spec name like ``step_completed`` into a
    callable that takes a parsed arg list and returns a
    bound :class:`Specification` instance.

The mini-language parser already knows how to dispatch
to the builtins; this module just bridges from
``SpecRegistry`` to the mini-language's expected interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._mini_lang import (
    BUILTIN_SPECS,
    evaluate,
    is_pure_name,
    NameLookup,
    parse_expression,
)
from .base import Specification, StepContext

if TYPE_CHECKING:
    pass


__all__ = [
    "MiniLangSpec",
    "SpecRegistry",
    "build_builtin_spec",
]


# ---------------------------------------------------------------------------
# MiniLangSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MiniLangSpec(Specification):
    """A :class:`Specification` that evaluates a parsed
    mini-language expression.

    .. warning::

        **DEV status -- not recommended for production use.**

        The mini-language grammar, built-in functions, and
        AST node shapes may change without notice in
        subsequent releases. For new code, prefer the
        :class:`Specification` API or the :class:`Composable`
        combinators (``AndSpec`` / ``OrSpec`` / ``NotSpec``).
        This class is the primary user-facing surface of the
        mini-language in Python; the bundle loader produces
        ``MiniLangSpec`` instances internally from the
        bundle's ``specifications:`` block.

    The expression is parsed once at construction; each
    ``is_satisfied_by`` call walks the AST against the
    current ``StepContext``. ``registry`` is used to
    resolve name lookups (e.g., ``ConfidencePassed`` →
    another spec). For pure name lookups the registry
    must provide the spec; for compound expressions the
    registry is optional (only needed if the expression
    references named specs).
    """

    expression: str
    _ast: Any = field(init=False, repr=False, compare=False)
    _registry: "SpecRegistry | None" = field(
        init=False, repr=False, compare=False, default=None
    )

    def __post_init__(self) -> None:
        # Use object.__setattr__ because the dataclass is
        # frozen and these are derived (not constructor)
        # fields. We could make them InitVar, but the
        # derived form is more honest about lifecycle.
        object.__setattr__(self, "_ast", parse_expression(self.expression))
        object.__setattr__(self, "_registry", None)

    def with_registry(self, registry: "SpecRegistry") -> "MiniLangSpec":
        """Return a new spec with the given registry bound.

        Specs are frozen; the registry is part of the
        runtime binding (which registry to use depends on
        the bundle being loaded), so we expose this as a
        copy method rather than a mutable field.
        """
        new = MiniLangSpec(self.expression)
        object.__setattr__(new, "_ast", self._ast)
        object.__setattr__(new, "_registry", registry)
        return new

    def _resolve_asts(self, ast: Any) -> Any:
        """Walk the AST and replace any NameLookup nodes with
        resolved specs from the registry. Returns a new AST
        with lookups replaced (or the original if none found)."""
        if isinstance(ast, NameLookup):
            if self._registry is None:
                return ast
            spec = self._registry.get(ast.name)
            if spec is None:
                return ast
            # Replace the NameLookup with the resolved spec
            return spec._ast  # type: ignore[return-value]
        # Recurse into known AST node types
        if hasattr(ast, "left") and hasattr(ast, "right"):
            return type(ast)(
                self._resolve_asts(ast.left),
                self._resolve_asts(ast.right),
            )
        if hasattr(ast, "operand") and hasattr(ast, "negated"):
            return type(ast)(
                self._resolve_asts(ast.operand),
                ast.negated,
            )
        return ast

    def is_satisfied_by(self, ctx: StepContext) -> bool:
        resolved = self._resolve_asts(self._ast)
        # If nothing changed, just evaluate directly
        if resolved is self._ast:
            return evaluate(self._ast, ctx)
        # Otherwise evaluate the resolved AST
        return evaluate(resolved, ctx)


# ---------------------------------------------------------------------------
# SpecRegistry
# ---------------------------------------------------------------------------


class SpecRegistry:
    """Name → mini-language expression map for one bundle.

    The loader populates this from the bundle's
    ``specifications:`` block. The FSM/Saga runtime
    resolves spec names (e.g., ``ConfidencePassed``) to
    :class:`Specification` instances via :meth:`resolve`.
    """

    def __init__(self) -> None:
        self._specs: dict[str, str] = {}

    def register(self, spec_id: str, expression: str) -> None:
        """Register a named predicate.

        Re-registration overwrites. The expression is
        validated syntactically at registration time so
        typos surface early.
        """
        # Parse to validate syntax. We discard the AST
        # (re-parsed on demand by MiniLangSpec).
        parse_expression(expression)
        self._specs[spec_id] = expression

    def has(self, spec_id: str) -> bool:
        return spec_id in self._specs

    def get_expression(self, spec_id: str) -> str | None:
        return self._specs.get(spec_id)

    def get(self, spec_id: str) -> "Specification | None":
        """Resolve a spec name to a :class:`Specification`.

        Returns ``None`` if the name is not registered.
        """
        expr = self._specs.get(spec_id)
        if expr is None:
            return None
        spec = MiniLangSpec(expr)
        return spec.with_registry(self)

    def resolve(self, expression: str) -> "Specification":
        """Resolve an expression (name or compound) to a spec.

        - If the expression is a pure name lookup AND
          that name is registered here, return the
          registered spec.
        - Otherwise, return a :class:`MiniLangSpec` that
          evaluates the expression (with this registry
          bound for any nested name lookups).
        """
        if is_pure_name(expression) is not None:
            name = is_pure_name(expression)
            if name in self._specs:
                # Resolve through the registry so the returned
                # spec benefits from the registry's other
                # registrations.
                return self.get(name)  # type: ignore[return-value]
        # Fall back: return a MiniLangSpec with this
        # registry bound (so nested name lookups resolve).
        return MiniLangSpec(expression).with_registry(self)


# ---------------------------------------------------------------------------
# Built-in spec factory
# ---------------------------------------------------------------------------


def build_builtin_spec(name: str, args: tuple[Any, ...]) -> Specification:
    """Build a :class:`Specification` from a built-in name
    and parsed argument list. Used by the mini-language
    evaluator when it sees a :class:`Call` AST node.

    For most builtins, args are coerced to ``str`` (they
    represent step names, field names, tool names, etc.).
    """
    if name not in BUILTIN_SPECS:
        raise ValueError(
            f"unknown built-in spec: {name!r}; available: {sorted(BUILTIN_SPECS)}"
        )
    factory = BUILTIN_SPECS[name]
    # Coerce args: most builtins take strings (step_name,
    # field name, tool name, tier). Booleans and integers
    # are accepted as-is for future flexibility (e.g.,
    # numeric comparisons in mini-language specs).
    coerced = tuple(
        str(a) if isinstance(a, (str, int, float, bool)) else a for a in args
    )
    return factory(*coerced)
