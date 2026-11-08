# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Architecture check — the dispatcher does not know about agents (ADR-088 §3.6).

ADR-088 §3.6: the dispatcher registers ``WorldSystem`` and
``WorldProjection`` instances and nothing else. It never reads a
definition, never consults a memory-tier declaration, and never
branches on an agent archetype.

Two rules:

1. No framework module names a type of the agent model. Stronger than
   the design gate's ``KNT-BOUND-001``, which exempts type-only
   imports — a framework signature mentioning ``AgentDefinition`` puts
   a vertical's vocabulary into the framework's public surface.
2. The dispatcher tier never reads ``RoleComponent``. No import rule
   covers this: ``RoleComponent`` is a framework type, so a
   dispatcher branching on the agent's persona would pass every
   boundary rule and still violate the decision.

Detection is by **AST**, not by text: a ``grep`` for
``kntgraph.agents`` would accuse the docstring of
``core/components/solution.py``, which only documents that the
vertical re-exports a framework symbol.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._collectors import SRC_ROOT, relative

#: The framework tier (ADR-088 §3.6 speaks of "the dispatcher and its
#: modules"; the whole framework tier is checked, because the agent
#: model must not leak into any of it).
FRAMEWORK_PACKAGES = frozenset(
    {
        "core",
        "tools",
        "infra",
        "stream",
        "security",
        "runner",
        "resilience",
        "concordos",
    }
)

#: Names that would betray agent-awareness in the framework.
FORBIDDEN_NAMES = frozenset(
    {
        "AgentDefinition",
        "AgentBundle",
        "RoleSpec",
        "RoleProjection",
    }
)


def _framework_files(tier: str | None = None) -> list[Path]:
    """The framework-tier Python files, optionally of one tier."""
    return [
        path
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
        and path.relative_to(SRC_ROOT).parts[0] in FRAMEWORK_PACKAGES
        and (tier is None or path.relative_to(SRC_ROOT).parts[0] == tier)
    ]


def _named_types(tree: ast.Module) -> set[str]:
    """Every type name the module mentions.

    Covers the three ways a name can appear: an import alias, a
    class definition, and any reference (an annotation or a runtime
    use).
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.alias)) and node.name is not None:
            names.add(node.name)
    return names


def test_the_framework_does_not_name_the_agent_model() -> None:
    """No framework module names the agent model's types.

    Covers imports, annotations and runtime references.
    """
    offenders: list[str] = []
    for path in _framework_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        leaked = sorted(_named_types(tree) & FORBIDDEN_NAMES)
        if leaked:
            offenders.append(f"{relative(path)} references {leaked}")

    assert not offenders, (
        "Framework modules referencing the agent model:\n"
        + "\n".join(f"  - {offender}" for offender in offenders)
        + "\n\nADR-088 §3.6: the dispatcher registers WorldSystem and "
        "WorldProjection instances. It never reads a definition, a "
        "persona or a memory-tier declaration."
    )


def test_the_dispatcher_does_not_read_the_role_component() -> None:
    """The dispatcher tier never references ``RoleComponent``.

    Reading the role component is the business of the tier that owns
    the agent's behaviour (``agents/role_systems/``) and of the tool
    gate (``tools/``). The dispatcher folds events and runs systems; it
    does not know what an agent is.
    """
    offenders: list[str] = []
    for path in _framework_files(tier="runner"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if "RoleComponent" in _named_types(tree):
            offenders.append(relative(path))

    assert not offenders, (
        "Dispatcher modules referencing RoleComponent:\n"
        + "\n".join(f"  - {offender}" for offender in offenders)
        + "\n\nADR-088 §3.6: the dispatcher does not know about agents. "
        "Gate 2 is read by the systems that request tools, not by the "
        "dispatcher."
    )
