# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli._scanner -- what a bounded context contains on disk.

The declarative agent model (ADR-088) makes the agent file a
*derivation*: the persona, the tool allow-list, the domain types
and the two execution cycles are read off the artifacts that
already exist in the context. The CLI therefore has to know what
the context holds before it can render the declaration.

The scanner reads the **AST** of each artifact, not its
filename. A user who renames a class, a tool or a constant is
followed correctly; a filename heuristic would not be -- the
CamelCase of ``invoice_fsm_concordo.py`` is not
``InvoiceFSMConcordo``.

The scanner is the single source of truth for the derivation.
``knt new agent`` and ``knt upgrade`` both go through it, so a
regenerated file and a newly created file are byte-identical.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from kntgraph.cli._project import snake_case

# The artifact directories a bounded context owns. The
# concordos join the agents, components, events, systems and
# tools: they are the context's behaviour, and the agent
# declaration references them.
TOOLS_DIR = "tools"
COMPONENTS_DIR = "components"
CONCORDOS_DIR = "concordos"
AGENTS_DIR = "agents"

# The role event the generated declaration listens for. The
# framework's ``RoleProjection`` reads the persona and the
# instructions from the event payload and falls back to the
# declared spec when the payload omits them (ADR-088 §2.6).
DEFAULT_ROLE_EVENT_TYPE = "role.assigned"


@dataclass(frozen=True, slots=True)
class ToolArtifact:
    """One ``@tool_worker`` tool declared in the context.

    Attributes:
        module: The dotted module path relative to the context
            package (e.g. ``"tools.process_payment"``).
        class_name: The class carrying the ``@tool_worker``
            decorator.
        tool_name: The name the tool is registered under. It is
            the name the agent's allow-list and the
            ``tool.<name>.requested`` event both use.
    """

    module: str
    class_name: str
    tool_name: str


@dataclass(frozen=True, slots=True)
class ComponentArtifact:
    """One domain component declared in the context.

    Attributes:
        module: The dotted module path relative to the context
            package.
        class_name: The component class. The agent's rules read
            it **by this exact class** (ADR-088 §3.3).
    """

    module: str
    class_name: str


@dataclass(frozen=True, slots=True)
class ConcordoArtifact:
    """One Concordo declared in the context.

    Attributes:
        module: The dotted module path relative to the context
            package.
        class_name: The name of the module-level instance (the
            template assigns it, e.g. ``InvoiceFSMConcordo``).
        kind: ``"fsm"`` or ``"saga"`` -- which of the two
            execution cycles the Concordo belongs to.
    """

    module: str
    class_name: str
    kind: str


@dataclass(frozen=True, slots=True)
class AgentArtifact:
    """One agent declaration in the context.

    Attributes:
        module: The dotted module path relative to the context
            package.
        constant: The module-level constant holding the
            ``AgentDefinition``.
        agent_name: The declaration's ``name`` -- the identity
            the dispatcher tracks.
    """

    module: str
    constant: str
    agent_name: str


@dataclass(frozen=True, slots=True)
class ContextInventory:
    """Everything one bounded context declares.

    Attributes:
        tools: The ``@tool_worker`` tools.
        components: The domain components.
        concordos: The FSM and Saga Concordos.
        agents: The agent declarations.
    """

    tools: tuple[ToolArtifact, ...] = ()
    components: tuple[ComponentArtifact, ...] = ()
    concordos: tuple[ConcordoArtifact, ...] = ()
    agents: tuple[AgentArtifact, ...] = ()

    def fsm(self) -> ConcordoArtifact | None:
        """The context's FSM Concordo, when it declares one.

        A context holds at most one FSM: the reactive cycle
        governs a single state machine. The first one wins,
        the way the ``ConcordoCatalog`` dedupes by name.
        """
        for concordo in self.concordos:
            if concordo.kind == "fsm":
                return concordo
        return None

    def sagas(self) -> tuple[ConcordoArtifact, ...]:
        """The context's Saga Concordos -- the deliberative cycle."""
        return tuple(c for c in self.concordos if c.kind == "saga")


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module | None:
    """Parse a Python file.

    Returns ``None`` when the file cannot be read or does not
    parse: a half-written artifact must not break the scan of
    the rest of the context.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _modules(directory: Path) -> list[Path]:
    """The importable modules of a directory, sorted by name."""
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".py" and path.stem != "__init__"
    )


def _call_name(node: ast.expr) -> str:
    """The bare name of a called expression (``Foo()`` -> ``Foo``)."""
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    return ""


def _dotted_name(node: ast.expr) -> str:
    """The last component of a dotted name (``a.b.C`` -> ``C``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _decorator_name(node: ast.expr) -> str:
    """The name of a decorator, with or without a call.

    ``@tool_worker`` and ``@tool_worker(name="x")`` both name
    ``tool_worker``: the decorator may or may not be called.
    """
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    return _dotted_name(node)


def _target_name(node: ast.Assign) -> str:
    """The name assigned by a module-level assignment."""
    if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return ""


def _keyword_value(call: ast.Call, name: str) -> str:
    """A string keyword argument of a call (``""`` when absent)."""
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, str):
                return value
    return ""


def _int_keyword_value(call: ast.Call, name: str, default: int) -> int:
    """An integer keyword argument of a call."""
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return default


def _snake(name: str) -> str:
    """CamelCase to snake_case (the CLI's naming convention)."""
    return snake_case(name)


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------


def _scan_tools(base: Path) -> tuple[ToolArtifact, ...]:
    """The ``@tool_worker`` tools declared in ``<context>/tools``."""
    found: list[ToolArtifact] = []
    for path in _modules(base / TOOLS_DIR):
        tree = _parse(path)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for decorator in node.decorator_list:
                if _decorator_name(decorator) != "tool_worker":
                    continue
                found.append(
                    ToolArtifact(
                        module=f"{TOOLS_DIR}.{path.stem}",
                        class_name=node.name,
                        tool_name=_tool_worker_name(decorator) or _snake(node.name),
                    )
                )
    return tuple(found)


def _tool_worker_name(decorator: ast.expr) -> str:
    """The ``name=`` argument of a ``@tool_worker(...)`` decorator."""
    if isinstance(decorator, ast.Call):
        return _keyword_value(decorator, "name")
    return ""


def _scan_components(base: Path) -> tuple[ComponentArtifact, ...]:
    """The domain components declared in ``<context>/components``."""
    found: list[ComponentArtifact] = []
    for path in _modules(base / COMPONENTS_DIR):
        tree = _parse(path)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not _is_domain_component(node):
                continue
            found.append(
                ComponentArtifact(
                    module=f"{COMPONENTS_DIR}.{path.stem}",
                    class_name=node.name,
                )
            )
    return tuple(found)


def _is_domain_component(node: ast.ClassDef) -> bool:
    """Whether the class derives from ``DomainComponent``."""
    return any(_dotted_name(base).endswith("DomainComponent") for base in node.bases)


def _scan_concordos(base: Path) -> tuple[ConcordoArtifact, ...]:
    """The Concordos declared in ``<context>/concordos``."""
    found: list[ConcordoArtifact] = []
    for path in _modules(base / CONCORDOS_DIR):
        tree = _parse(path)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            kind = _concordo_kind(node.value)
            if kind is None:
                continue
            found.append(
                ConcordoArtifact(
                    module=f"{CONCORDOS_DIR}.{path.stem}",
                    class_name=_target_name(node),
                    kind=kind,
                )
            )
    return tuple(found)


def _concordo_kind(value: ast.expr) -> str | None:
    """The execution cycle a Concordo belongs to."""
    if not isinstance(value, ast.Call):
        return None
    name = _dotted_name(value.func)
    if name == "BusinessFSMConcordo":
        return "fsm"
    if name == "WorkflowSagaConcordo":
        return "saga"
    return None


def _scan_agents(base: Path) -> tuple[AgentArtifact, ...]:
    """The agent declarations found in ``<context>/agents``."""
    found: list[AgentArtifact] = []
    for path in _modules(base / AGENTS_DIR):
        tree = _parse(path)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if _call_name(node.value) != "AgentDefinition":
                continue
            found.append(
                AgentArtifact(
                    module=f"{AGENTS_DIR}.{path.stem}",
                    constant=_target_name(node),
                    agent_name=_agent_definition_name(node.value) or path.stem,
                )
            )
    return tuple(found)


def _agent_definition_name(value: ast.expr) -> str:
    """The ``name=`` argument of an ``AgentDefinition(...)`` call."""
    if isinstance(value, ast.Call):
        return _keyword_value(value, "name")
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_context(package: str, context: str) -> ContextInventory:
    """Scan one bounded context.

    Args:
        package: The project package (the single directory under
            ``src/``).
        context: The bounded context name.

    Returns:
        The artifacts the context declares. An absent
        directory yields an empty inventory -- a context that
        has not been scaffolded yet is not an error.
    """
    base = Path("src") / package / "contexts" / context
    return ContextInventory(
        tools=_scan_tools(base),
        components=_scan_components(base),
        concordos=_scan_concordos(base),
        agents=_scan_agents(base),
    )


# ---------------------------------------------------------------------------
# Path-based discovery
# ---------------------------------------------------------------------------
#
# The AST scanner above answers "what does the context
# *declare*?". The discovery below answers the path-based
# question the ``knt upgrade`` machinery asks: "which files
# under the context are the CLI's?".


def discover_package_name() -> str | None:
    """Infer the package name from the project root."""
    src_dir = Path("src")
    if not src_dir.is_dir():
        return None
    packages = [
        d
        for d in src_dir.iterdir()
        if d.is_dir() and d.name != "__pycache__" and not d.name.startswith(".")
    ]
    if not packages:
        return None
    return packages[0].name


def discover_context_names() -> list[str]:
    """List the Bounded Contexts under ``src/<package>/contexts/``."""
    src_dir = Path("src")
    if not src_dir.is_dir():
        return []
    package = discover_package_name()
    if package is None:
        return []
    contexts_dir = src_dir / package / "contexts"
    if not contexts_dir.is_dir():
        return []
    return sorted(
        d.name
        for d in contexts_dir.iterdir()
        if d.is_dir() and d.name != "__pycache__" and not d.name.startswith(".")
    )


def discover_artifacts(context_name: str) -> dict[str, list[str]]:
    """List the existing artifacts (agent / event / system / tool / component)
    for a given context. Returns a dict mapping
    ``{artifact_kind: [filename_without_extension]}``.
    """
    package = discover_package_name()
    if package is None:
        return {}
    base = Path("src") / package / "contexts" / context_name
    artifacts: dict[str, list[str]] = {
        "agents": [],
        "events": [],
        "systems": [],
        "tools": [],
        "components": [],
    }
    for kind, kind_dir in ((k, base / k) for k in artifacts):
        if not kind_dir.is_dir():
            continue
        for f in kind_dir.iterdir():
            if f.is_file() and f.suffix == ".py" and f.stem != "__init__":
                artifacts[kind].append(f.stem)
    return artifacts


def has_supervisor_artifact(context_name: str) -> bool:
    """Whether the context has a supervisor agent artifact."""
    package = discover_package_name()
    if package is None:
        return False
    agents_dir = Path("src") / package / "contexts" / context_name / "agents"
    if not agents_dir.is_dir():
        return False
    return any(f.is_file() and "_supervisor" in f.stem for f in agents_dir.iterdir())
