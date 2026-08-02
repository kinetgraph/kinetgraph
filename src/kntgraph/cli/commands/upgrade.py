# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli.commands.upgrade -- ``knt upgrade`` (ADR-053).

The ``knt new <artifact>`` commands render Jinja templates
into ``src/<package>/contexts/<context>/...`` files. When the
framework evolves (e.g. ADR-052 added the ``consumer.py``
template, ADR-053 added the ``config.py`` template), the
boilerplate on disk goes stale: the user has to either
``git rm`` the old file and re-run ``knt new`` (losing
local edits) or hand-merge the diff (error-prone).

``knt upgrade`` is the canonical entry point for the
**post-init regeneration** workflow. It supports three
operating modes:

  - ``knt upgrade --check``: report which generated boilerplate
    files are out of date **without** writing anything. Exit
    code 0 when no drift is detected, 1 when drift is found.
    Use this in CI to detect bots that edited boilerplate
    without running the upgrade.

  - ``knt upgrade --apply <relative_path>``: regenerate a
    single file. The relative path is from the project root,
    e.g. ``src/myapp/consumer.py``. The command renders the
    current template with the discovered context and writes
    the result. **Local edits are lost** unless the template
    supports a merge mode (currently none).

  - ``knt upgrade --apply-all --force``: regenerate every
    boilerplate file that has a corresponding template. The
    ``--force`` flag is mandatory because the operation
    overwrites without diff confirmation.

The discovery of "which files are boilerplate" is **path-based**:
files under ``src/<package>/contexts/<context>/{agents,components,
events,systems,tools}/`` plus the synthesised root files
(``<context>/dispatcher.py``, ``<project>/consumer.py``,
``<project>/core/config.py``, ``<project>/main.py``). The
``--list-templates`` flag prints the resolved list for
debugging.

The CLI does NOT touch user-modified application code. The
template renderer is purely substitutional and never inspects
the user's logical changes -- it only knows the template
contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from jinja2 import Environment, FileSystemLoader


app = typer.Typer(
    help="Regenerate boilerplate files against the current templates.",
)
console = Console()


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


# The **canonical** mapping of (template_name, rendered_path,
# default_context). ``rendered_path`` is relative to the project
# root. ``default_context`` is a callable that takes the
# package_name + context_name + opts and returns the context
# dict the jinja template expects.
#
# This is the **only** place the upgrade machinery knows about
# boilerplate locations. Adding a new template means adding one
# entry here; the ``knt upgrade`` CLI picks it up automatically.
@dataclass(frozen=True)
class _BoilerplateMapping:
    template_name: str
    rendered_path: str  # relative to project root
    requires_package: bool = True
    requires_context: bool = False


# Path templates use ``{package}`` and ``{context}`` placeholders.
# The rendered path uses the same placeholders.
_MAPPING: tuple[_BoilerplateMapping, ...] = (
    # Root-level files (no context).
    _BoilerplateMapping(
        template_name="main.py.jinja",
        rendered_path="src/{package}/main.py",
        requires_package=True,
        requires_context=False,
    ),
    _BoilerplateMapping(
        template_name="config.py.jinja",
        rendered_path="src/{package}/core/config.py",
        requires_package=True,
        requires_context=False,
    ),
    _BoilerplateMapping(
        template_name="consumer.py.jinja",
        rendered_path="src/{package}/consumer.py",
        requires_package=True,
        requires_context=False,
    ),
    # Per-context files.
    _BoilerplateMapping(
        template_name="dispatcher.py.jinja",
        rendered_path="src/{package}/contexts/{context}/dispatcher.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="agent.py.jinja",
        rendered_path="src/{package}/contexts/{context}/agents/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="event.py.jinja",
        rendered_path="src/{package}/contexts/{context}/events/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="system.py.jinja",
        rendered_path="src/{package}/contexts/{context}/systems/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="tool.py.jinja",
        rendered_path="src/{package}/contexts/{context}/tools/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="component.py.jinja",
        rendered_path="src/{package}/contexts/{context}/components/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
)


def _discover_package_name() -> str | None:
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


def _discover_context_names() -> list[str]:
    """List the Bounded Contexts under ``src/<package>/contexts/``."""
    src_dir = Path("src")
    if not src_dir.is_dir():
        return []
    package = _discover_package_name()
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


def _discover_artifacts(context_name: str) -> dict[str, list[str]]:
    """List the existing artifacts (agent / event / system / tool / component)
    for a given context. Returns a dict mapping
    ``{artifact_kind: [filename_without_extension]}``.
    """
    package = _discover_package_name()
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
    for kind in artifacts:
        kind_dir = base / kind
        if not kind_dir.is_dir():
            continue
        for f in kind_dir.iterdir():
            if f.is_file() and f.suffix == ".py" and f.stem != "__init__":
                artifacts[kind].append(f.stem)
    return artifacts


def _base_context(package: str, project_name: str) -> dict[str, Any]:
    """The minimum context shared by every template."""
    return {
        "project_name": project_name,
        "package": package,
    }


def _render_template(
    template_name: str,
    context: dict[str, Any],
) -> str:
    """Render a template by name with the given context."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=False,  # nosec B701
    )
    tmpl = env.get_template(template_name)
    return tmpl.render(context)


def _resolve_mappings() -> list[tuple[Path, str, dict[str, Any]]]:
    """Resolve every boilerplate mapping to a concrete (path, template, ctx)
    triple. Returns the list of files that ``knt upgrade`` can act on.

    The function is a thin orchestrator: it gathers the
    package-level context (project flags, context names)
    and dispatches per-mapping to a small helper. Each
    helper focuses on one mapping shape (project-level /
    per-context / per-artifact). The CC stays below 10
    because the branching lives in the per-shape helpers
    (AGENTS.md §3.2 + DEBT §2.26 ``CC offenders``).
    """
    package = _discover_package_name()
    if package is None:
        return []
    project_name = package  # convention: project name == package name

    init_flags = _infer_init_flags(package)
    context_names = _discover_context_names()

    resolved: list[tuple[Path, str, dict[str, Any]]] = []
    for mapping in _MAPPING:
        if not mapping.requires_package:
            continue
        resolved.extend(
            _resolve_mapping(
                mapping=mapping,
                package=package,
                project_name=project_name,
                init_flags=init_flags,
                context_names=context_names,
            )
        )
    return resolved


def _infer_init_flags(
    package: str,
) -> dict[str, Any]:
    """Infer the ``init.py`` flags from the generated
    ``main.py``.

    The project-level templates (``main.py.jinja``,
    ``consumer.py.jinja``) require ``use_intent_http``
    and ``routing_mode``. The values are recovered
    heuristically from the existing ``main.py``:

    - ``use_intent_http``: True when the file imports
      ``from kntgraph.api import create_app`` (the HTTP
      path imports it; the routing-only path does not).
    - ``routing_mode``: parsed from the
      ``routing.adapters.<mode> import`` line. Defaults
      to ``"external"`` when the snippet is absent
      (e.g. the HTTP path does not import the adapter).
    """
    main_path = Path("src") / package / "main.py"
    use_intent_http = False
    routing_mode = "external"
    if not main_path.exists():
        return {
            "use_intent_http": use_intent_http,
            "routing_mode": routing_mode,
        }
    text = main_path.read_text(encoding="utf-8")
    use_intent_http = "from kntgraph.api import create_app" in text
    m = re.search(r"routing\.adapters\.(\w+)\s+import", text)
    if m:
        routing_mode = m.group(1)
    return {
        "use_intent_http": use_intent_http,
        "routing_mode": routing_mode,
    }


def _resolve_mapping(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    project_name: str,
    init_flags: dict[str, Any],
    context_names: list[str],
) -> list[tuple[Path, str, dict[str, Any]]]:
    """Dispatch one mapping to the right per-shape helper.

    The shape is determined by two flags on the mapping:

    - ``requires_context``: per-context (one file per
      context, e.g. ``dispatcher.py``) or per-artifact
      (one file per artifact within a context, e.g.
      ``agents``, ``events``).
    - ``requires_package`` and ``template_name``:
      project-level files (e.g. ``main.py``,
      ``config.py``, ``consumer.py``) share the
      ``init_flags`` context.
    """
    if mapping.requires_context:
        return _resolve_context_mapping(
            mapping=mapping,
            package=package,
            context_names=context_names,
            base_ctx=_base_context(package, project_name),
        )
    return _resolve_project_mapping(
        mapping=mapping,
        package=package,
        base_ctx=_base_context(package, project_name),
        init_flags=init_flags,
    )


def _resolve_project_mapping(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    base_ctx: dict[str, Any],
    init_flags: dict[str, Any],
) -> list[tuple[Path, str, dict[str, Any]]]:
    """Project-level boilerplate (``main.py``,
    ``consumer.py``, ``config.py``). The ``main.py.jinja``
    and ``consumer.py.jinja`` templates consume the
    ``init_flags``; ``config.py.jinja`` does not.
    """
    ctx = base_ctx
    if mapping.template_name in ("main.py.jinja", "consumer.py.jinja"):
        ctx = {**ctx, **init_flags}
    return [
        (
            Path(mapping.rendered_path.format(package=package)),
            mapping.template_name,
            ctx,
        ),
    ]


def _resolve_context_mapping(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    context_names: list[str],
    base_ctx: dict[str, Any],
) -> list[tuple[Path, str, dict[str, Any]]]:
    """Per-context boilerplate. For per-artifact templates
    (``agent.py.jinja``, ``event.py.jinja``, etc.) we
    emit one entry per existing artifact; for singleton
    templates (``dispatcher.py.jinja``) we emit one entry
    per context.
    """
    resolved: list[tuple[Path, str, dict[str, Any]]] = []
    kind = _template_to_kind(mapping.template_name)
    for context_name in context_names:
        ctx_with_c = {**base_ctx, "context_name": context_name}
        if kind is None:
            resolved.append(
                _resolve_context_singleton(
                    mapping=mapping,
                    package=package,
                    context_name=context_name,
                    ctx_with_c=ctx_with_c,
                ),
            )
            continue
        resolved.extend(
            _resolve_context_artifacts(
                mapping=mapping,
                package=package,
                context_name=context_name,
                kind=kind,
                ctx_with_c=ctx_with_c,
            )
        )
    return resolved


def _resolve_context_singleton(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    context_name: str,
    ctx_with_c: dict[str, Any],
) -> tuple[Path, str, dict[str, Any]]:
    """One entry per context (e.g. ``dispatcher.py``)."""
    return (
        Path(
            mapping.rendered_path.format(
                package=package,
                context=context_name,
            ),
        ),
        mapping.template_name,
        {
            **ctx_with_c,
            "with_supervisor": _has_supervisor_artifact(context_name),
        },
    )


def _resolve_context_artifacts(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    context_name: str,
    kind: str,
    ctx_with_c: dict[str, Any],
) -> list[tuple[Path, str, dict[str, Any]]]:
    """One entry per existing artifact inside the context
    (e.g. every ``agents/<name>.py``, every
    ``events/<name>.py``). The ``<name>`` placeholder in
    the mapping's ``rendered_path`` is substituted with
    the artifact's filename stem.
    """
    resolved: list[tuple[Path, str, dict[str, Any]]] = []
    for artifact in _discover_artifacts(context_name).get(kind, []):
        # The ``agent.py.jinja`` expects ``camel_case_name``
        # (the original CamelCase input). The on-disk
        # name is ``agent_name`` (snake_case). The
        # discoverer doesn't know the original CamelCase
        # form -- we synthesise it from the filename.
        artifact_ctx = {
            **ctx_with_c,
            "agent_name": artifact,
            "system_name": artifact,
            "tool_name": artifact,
            "event_name": artifact,
            "camel_case_name": _snake_to_camel(artifact),
            "event_type": f"{context_name}.{artifact}",
            "with_supervisor": _has_supervisor_artifact(context_name),
        }
        resolved.append(
            (
                Path(
                    mapping.rendered_path.format(
                        package=package,
                        context=context_name,
                    ).replace("<name>", artifact),
                ),
                mapping.template_name,
                artifact_ctx,
            ),
        )
    return resolved


def _template_to_kind(template_name: str) -> str | None:
    """Map a template name to its artifact kind (``agents``,
    ``events``, ``systems``, ``tools``, ``components``). Returns
    ``None`` for templates that are not per-artifact.
    """
    return {
        "agent.py.jinja": "agents",
        "event.py.jinja": "events",
        "system.py.jinja": "systems",
        "tool.py.jinja": "tools",
        "component.py.jinja": "components",
    }.get(template_name)


def _snake_to_camel(snake_str: str) -> str:
    """Convert ``snake_case`` to ``CamelCase``. The CLI's
    ``new <artifact>`` accepts CamelCase and converts to
    snake_case for the filename; the round-trip is lossy
    (CamelCase cannot be recovered from snake_case
    unambiguously). We use a simple heuristic for the
    upgrade path: split on ``_`` and capitalise each part.
    """
    parts = snake_str.split("_")
    return "".join(p.capitalize() for p in parts)


def _has_supervisor_artifact(context_name: str) -> bool:
    """Whether the context has a supervisor agent artifact."""
    package = _discover_package_name()
    if package is None:
        return False
    agents_dir = Path("src") / package / "contexts" / context_name / "agents"
    if not agents_dir.is_dir():
        return False
    for f in agents_dir.iterdir():
        if f.is_file() and "_supervisor" in f.stem:
            return True
    return False


# ------------------------------------------------------------------
# CLI surface
# ------------------------------------------------------------------


@app.command()
def list_templates() -> None:
    """Print the resolved list of boilerplate mappings.

    For each (template_name, rendered_path) the command
    renders the current template with the discovered context
    and prints the resulting *target* path. Useful for
    debugging the upgrade machinery.
    """
    rows = _resolve_mappings()
    if not rows:
        console.print(
            "[red]Error:[/red] No boilerplate found. Are you "
            "in a Kinetgraph project root?",
        )
        raise typer.Exit(code=1)
    table = Table("Target path", "Template", title="Boilerplate")
    for path, template_name, _ctx in rows:
        table.add_row(str(path), template_name)
    console.print(table)


@app.command()
def check() -> None:
    """Report which generated boilerplate files are out of date.

    Exit code 0 when no drift is detected, 1 when drift is
    found. ``--quiet`` suppresses the per-file output (only
    the summary is printed); useful in CI.
    """
    rows = _resolve_mappings()
    if not rows:
        console.print(
            "[red]Error:[/red] No boilerplate found. Are you "
            "in a Kinetgraph project root?",
        )
        raise typer.Exit(code=1)

    drifted: list[tuple[Path, str, str]] = []
    for path, template_name, ctx in rows:
        if not path.exists():
            drifted.append((path, template_name, "missing"))
            continue
        current = path.read_text(encoding="utf-8")
        expected = _render_template(template_name, ctx)
        if current != expected:
            drifted.append((path, template_name, "drifted"))

    if drifted:
        table = Table("Path", "Template", "Status", title="Drift detected")
        for path, template_name, status in drifted:
            table.add_row(str(path), template_name, status)
        console.print(table)
        console.print(
            f"[yellow]{len(drifted)} file(s) drifted. "
            f"Run 'knt upgrade apply-all' to regenerate.[/yellow]",
        )
        raise typer.Exit(code=1)
    console.print("[green]No drift detected.[/green]")


@app.command()
def apply(
    target: str = typer.Argument(
        ...,
        help="Relative path of the file to regenerate, e.g. src/myapp/consumer.py",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite the file even if it has local modifications.",
    ),
) -> None:
    """Regenerate a single boilerplate file."""
    rows = _resolve_mappings()
    target_path = Path(target)
    match = next(
        (r for r in rows if r[0] == target_path),
        None,
    )
    if match is None:
        console.print(
            f"[red]Error:[/red] {target!r} is not a known "
            f"boilerplate file. Run 'knt upgrade list-templates'.",
        )
        raise typer.Exit(code=1)

    path, template_name, ctx = match
    expected = _render_template(template_name, ctx)

    if not force and path.exists():
        current = path.read_text(encoding="utf-8")
        if current != expected:
            console.print(
                f"[red]Error:[/red] {path} has local modifications. "
                f"Re-run with --force to overwrite.",
            )
            raise typer.Exit(code=1)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8")
    console.print(f"[green]Regenerated[/green] {path}")


@app.command()
def apply_all(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite files even if they have local modifications.",
    ),
) -> None:
    """Regenerate every boilerplate file."""
    rows = _resolve_mappings()
    if not rows:
        console.print(
            "[red]Error:[/red] No boilerplate found. Are you "
            "in a Kinetgraph project root?",
        )
        raise typer.Exit(code=1)

    regenerated = 0
    skipped = 0
    for path, template_name, ctx in rows:
        expected = _render_template(template_name, ctx)
        if path.exists():
            current = path.read_text(encoding="utf-8")
            if current != expected and not force:
                console.print(
                    f"[yellow]Skip[/yellow] {path} (local modifications; "
                    f"pass --force to overwrite)",
                )
                skipped += 1
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        console.print(f"[green]Regenerated[/green] {path}")
        regenerated += 1
    console.print(
        f"Done: {regenerated} regenerated, {skipped} skipped.",
    )


__all__ = ["app", "list_templates", "check", "apply", "apply_all"]
