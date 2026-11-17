# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli._derive -- what the CLI derives from a bounded context.

The declarative agent model (ADR-088) makes two of the CLI's
files **derived**: the agent declaration and the context's
dispatcher. Both are rendered from what the context holds on
disk -- the tools, the components, the Concordos and the other
agents.

This module holds that derivation:

  1. The template contexts (:func:`agent_context`,
     :func:`dispatcher_context`).
  2. The safe refresh (:func:`refresh_derived_files`), which
     rewrites a derived file **only** when the user has not
     edited it.

The refresh rule is what makes the derivation usable. A derived
file that the user edited is left alone -- the CLI prints a
hint instead of clobbering hand-written wiring.
"""

from __future__ import annotations

from pathlib import Path

from kntgraph.cli._scanner import (
    AGENTS_DIR,
    ContextInventory,
    scan_context,
)
from kntgraph.cli._templates import render_template
from kntgraph.core._typing import JsonValue

# The templates the CLI derives files from.
AGENT_TEMPLATE = "agent.py.jinja"
DISPATCHER_TEMPLATE = "dispatcher.py.jinja"


def _imports(entries: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """Cast a list of import descriptors for the template context."""
    return list(entries)


def _import_block(
    entries: list[dict[str, JsonValue]], name_key: str = "class_name"
) -> str:
    """The local import block of a derived file.

    The block is pre-rendered so the template's whitespace is
    deterministic: a file with local imports and a file without
    them differ by exactly the import lines.

    Args:
        entries: The import descriptors.
        name_key: The key holding the imported name.

    Returns:
        The import lines, each terminated by a newline. An empty
        string when there is nothing to import.
    """
    lines = [f"from {entry['module']} import {entry[name_key]}" for entry in entries]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _tuple_literal(parts: list[str], indent: str) -> str:
    """A Python tuple literal for the given elements.

    An empty tuple renders as ``()``. A one-element tuple keeps
    its trailing comma -- ``(X,)`` is a tuple, ``(X)`` is not.
    A literal that would overflow the line length is wrapped
    one element per line.

    Args:
        parts: The rendered elements (identifiers or string
            literals).
        indent: The indentation of the line the literal sits on.

    Returns:
        The literal, ready to be spliced into the template.
    """
    if not parts:
        return "()"
    one_line = "(" + ", ".join(parts) + ",)"
    if len(one_line) + len(indent) <= 88:
        return one_line
    body = "".join(f"\n{indent}    {part}," for part in parts)
    return "(" + body + f"\n{indent})"


def agent_context(
    inventory: ContextInventory,
    *,
    agent_name: str,
    persona: str,
    role_event_type: str = "role.assigned",
) -> dict[str, JsonValue]:
    """Build the template context of an agent declaration.

    The declaration is *derived* from the context: the tool
    allow-list is the context's tools, the domain types are the
    context's components, and the two execution cycles are the
    context's Concordos.

    Args:
        inventory: What the context declares.
        agent_name: The declaration's name.
        persona: The persona the role spec declares.
        role_event_type: The event type that assigns the role
            at runtime.

    Returns:
        The Jinja context for ``agent.py.jinja``.
    """
    fsm = inventory.fsm()
    component_imports: list[dict[str, JsonValue]] = [
        {
            "module": f"..{component.module}",
            "class_name": component.class_name,
        }
        for component in inventory.components
    ]
    saga_imports: list[dict[str, JsonValue]] = [
        {
            "module": f"..{saga.module}",
            "class_name": saga.class_name,
        }
        for saga in inventory.sagas()
    ]
    fsm_import: dict[str, JsonValue] | None = (
        {
            "module": f"..{fsm.module}",
            "class_name": fsm.class_name,
        }
        if fsm is not None
        else None
    )
    return {
        "agent_name": agent_name,
        "constant_name": constant_name(agent_name),
        "persona": persona,
        "role_event_type": role_event_type,
        "allowed_tools": _tuple_literal(
            [f'"{tool.tool_name}"' for tool in inventory.tools],
            "        ",
        ),
        "component_imports": _imports(component_imports),
        "domain_types": _tuple_literal(
            [component.class_name for component in inventory.components],
            "    ",
        ),
        "fsm_import": fsm_import,
        "saga_imports": _imports(saga_imports),
        "sagas": _tuple_literal(
            [saga.class_name for saga in inventory.sagas()],
            "    ",
        ),
        "local_imports": _import_block(
            [
                *component_imports,
                *([fsm_import] if fsm_import is not None else []),
                *saga_imports,
            ]
        ),
    }


def dispatcher_context(
    inventory: ContextInventory,
    *,
    context_name: str,
) -> dict[str, JsonValue]:
    """Build the template context of a context's dispatcher.

    The dispatcher registers the context's tools with the worker
    manager and installs the context's agent declarations. It
    does not aggregate systems by hand any more: the runtime is
    derived from the declarations.

    Args:
        inventory: What the context declares.
        context_name: The bounded context name. The scaffold
            derives the principal's tenant from it.

    Returns:
        The Jinja context for ``dispatcher.py.jinja``.
    """
    tool_imports: list[dict[str, JsonValue]] = [
        {
            "module": f".{tool.module}",
            "class_name": tool.class_name,
        }
        for tool in inventory.tools
    ]
    agent_imports: list[dict[str, JsonValue]] = [
        {
            "module": f".{agent.module}",
            "constant": agent.constant,
            "agent_name": agent.agent_name,
        }
        for agent in inventory.agents
    ]
    return {
        "context_name": context_name,
        "tool_imports": _imports(tool_imports),
        "agent_imports": _imports(agent_imports),
        "local_imports": _import_block(
            [
                *tool_imports,
                *[
                    {
                        "module": agent["module"],
                        "class_name": agent["constant"],
                    }
                    for agent in agent_imports
                ],
            ]
        ),
        "tool_registrations": _registration_block(tool_imports),
        "agent_installs": _install_block(agent_imports, context_name),
    }


def _registration_block(tool_imports: list[dict[str, JsonValue]]) -> str:
    """The lines that register the context's tools.

    Args:
        tool_imports: The tool import descriptors.

    Returns:
        The registration lines, each terminated by a newline.
    """
    if not tool_imports:
        return ""
    lines: list[str] = [
        f"    worker_manager.register({tool['class_name']}(), acl=default_acl())"
        for tool in tool_imports
    ]
    return "\n".join(lines) + "\n"


def _install_block(agent_imports: list[dict[str, JsonValue]], context_name: str) -> str:
    """The lines that install the context's agent declarations.

    Args:
        agent_imports: The agent import descriptors.
        context_name: The bounded context name. The scaffold
            derives the principal's tenant from it.

    Returns:
        The installation lines, each terminated by a newline.
    """
    if not agent_imports:
        return ""
    blocks: list[str] = []
    for agent in agent_imports:
        blocks.append(
            f"    {agent['constant']}.install(\n"
            f"        dispatcher,\n"
            f"        principal=Principal.from_agent_id(\n"
            f'            "{context_name}.{agent["agent_name"]}",\n'
            f"            level=PrincipalLevel.agent,\n"
            f'            key_id="local-dev",\n'
            f"        ),\n"
            f"    )\n"
        )
    return "\n".join(blocks)


def constant_name(agent_name: str) -> str:
    """The module-level constant name of an agent declaration."""
    return f"{agent_name.upper()}_AGENT"


def resolve_dispatcher_context(package: str, context_name: str) -> dict[str, JsonValue]:
    """The template context of a context's dispatcher.

    Args:
        package: The project package.
        context_name: The bounded context.

    Returns:
        The Jinja context for ``dispatcher.py.jinja``.
    """
    inventory = scan_context(package, context_name)
    return dispatcher_context(inventory, context_name=context_name)


def resolve_agent_context(
    package: str,
    context_name: str,
    artifact: str,
) -> dict[str, JsonValue]:
    """The template context of an agent declaration.

    Args:
        package: The project package.
        context_name: The bounded context.
        artifact: The agent file's stem (e.g. ``checkout_agent``).

    Returns:
        The Jinja context for ``agent.py.jinja``.
    """
    inventory = scan_context(package, context_name)
    declaration_name = _declaration_name(inventory, artifact)
    return agent_context(
        inventory,
        agent_name=declaration_name,
        persona=declaration_name,
    )


def _declaration_name(inventory: ContextInventory, artifact: str) -> str:
    """The declaration's name of an agent artifact.

    The file stem carries the ``_agent`` suffix; the declaration
    itself is named without it. When the file already declares a
    name, that name wins.
    """
    for agent in inventory.agents:
        if agent.module.endswith(f".{artifact}"):
            return agent.agent_name
    return artifact.removesuffix("_agent")


def _without(
    inventory: ContextInventory,
    exclude_module: str | None,
) -> ContextInventory:
    """The inventory without one artifact.

    The inventory *before* the artifact that was just added is
    what the file on disk was rendered from -- the comparison
    that decides whether the file is pristine.
    """
    if exclude_module is None:
        return inventory
    return ContextInventory(
        tools=tuple(t for t in inventory.tools if t.module != exclude_module),
        components=tuple(c for c in inventory.components if c.module != exclude_module),
        concordos=tuple(c for c in inventory.concordos if c.module != exclude_module),
        agents=tuple(a for a in inventory.agents if a.module != exclude_module),
    )


def _is_pristine(path: Path, template: str, previous: dict[str, JsonValue]) -> bool:
    """Whether the file on disk is the template's untouched output."""
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8") == render_template(template, previous)


def refresh_derived_files(
    package: str,
    context: str,
    *,
    exclude_module: str | None = None,
) -> tuple[list[str], list[str]]:
    """Refresh the context's derived files after an artifact was added.

    A derived file is rewritten **only** when it is still the
    template's untouched output for the state *before* the new
    artifact. A file the user edited is left alone -- the
    caller prints a hint and the operator decides.

    Args:
        package: The project package.
        context: The bounded context.
        exclude_module: The dotted module path (relative to the
            context package) of the artifact that was just added.
            The previous rendering is computed without it.

    Returns:
        The rewritten file paths and the skipped ones.
    """
    inventory = scan_context(package, context)
    previous = _without(inventory, exclude_module)
    rewritten: list[str] = []
    skipped: list[str] = []

    dispatcher = Path("src") / package / "contexts" / context / "dispatcher.py"
    _refresh_one(
        dispatcher,
        DISPATCHER_TEMPLATE,
        dispatcher_context(inventory, context_name=context),
        dispatcher_context(previous, context_name=context),
        rewritten,
        skipped,
    )

    agents_dir = Path("src") / package / "contexts" / context / AGENTS_DIR
    for agent in inventory.agents:
        if agent.module == exclude_module:
            continue
        path = agents_dir / f"{agent.module.split('.')[-1]}.py"
        _refresh_one(
            path,
            AGENT_TEMPLATE,
            agent_context(
                inventory,
                agent_name=agent.agent_name,
                persona=agent.agent_name,
            ),
            agent_context(
                previous,
                agent_name=agent.agent_name,
                persona=agent.agent_name,
            ),
            rewritten,
            skipped,
        )
    return rewritten, skipped


def _refresh_one(
    path: Path,
    template: str,
    current: dict[str, JsonValue],
    previous: dict[str, JsonValue],
    rewritten: list[str],
    skipped: list[str],
) -> None:
    """Refresh one derived file, recording the outcome."""
    if not path.is_file():
        return
    expected = render_template(template, current)
    if path.read_text(encoding="utf-8") == expected:
        return
    if not _is_pristine(path, template, previous):
        skipped.append(str(path))
        return
    path.write_text(expected, encoding="utf-8")
    rewritten.append(str(path))


__all__ = [
    "AGENT_TEMPLATE",
    "DISPATCHER_TEMPLATE",
    "agent_context",
    "constant_name",
    "dispatcher_context",
    "refresh_derived_files",
    "resolve_agent_context",
    "resolve_dispatcher_context",
]
