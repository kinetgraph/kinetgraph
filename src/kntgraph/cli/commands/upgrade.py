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

import json
import sys
from dataclasses import dataclass
from pathlib import Path
import re
import typer
from rich.console import Console
from rich.table import Table
from jinja2 import Environment, FileSystemLoader

from kntgraph.cli._templates import render_template


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
        d for d in src_dir.iterdir()
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
        d.name for d in contexts_dir.iterdir()
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


def _base_context(package: str, project_name: str) -> dict[str, str]:
    """The minimum context shared by every template."""
    return {
        "project_name": project_name,
        "package": package,
    }


def _render_template(
    template_name: str,
    context: dict[str, str],
) -> str:
    """Render a template by name with the given context."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=False,  # nosec B701
    )
    tmpl = env.get_template(template_name)
    return tmpl.render(context)


def _resolve_mappings() -> list[tuple[Path, str, dict[str, str]]]:
    """Resolve every boilerplate mapping to a concrete (path, template, ctx)
    triple. Returns the list of files that ``knt upgrade`` can act on.
    """
    package = _discover_package_name()
    if package is None:
        return []
    project_name = package  # convention: project name == package name

    # The project-level templates (``main.py.jinja``,
    # ``consumer.py.jinja``) require the init's
    # ``use_intent_http`` and ``routing_mode`` flags.
    # We infer ``use_intent_http`` from the presence of
    # ``from kntgraph.api import create_app`` in the
    # existing ``main.py`` (the HTTP path imports it;
    # the routing-only path does not).
    main_path = Path("src") / package / "main.py"
    use_intent_http = False
    if main_path.exists():
        use_intent_http = "from kntgraph.api import create_app" in main_path.read_text()

    # The ``routing_mode`` is harder to recover: the
    # generated ``main.py`` references the adapter
    # module by name (``from <package>.routing.adapters.
    # <mode> import build_adapter``). We parse the
    # adapter import.
    routing_mode = "external"
    if main_path.exists():
        import re
        m = re.search(
            r"routing\.adapters\.(\w+)\s+import",
            main_path.read_text(),
        )
        if m:
            routing_mode = m.group(1)

    resolved: list[tuple[Path, str, dict[str, str]]] = []
    for mapping in _MAPPING:
        if not mapping.requires_package:
            continue
        ctx = _base_context(package, project_name)
        # The project-level templates that depend on the
        # init's flags share the ``use_intent_http`` /
        # ``routing_mode`` context.
        if mapping.template_name in ("main.py.jinja", "consumer.py.jinja"):
            ctx = {**ctx, "use_intent_http": use_intent_http, "routing_mode": routing_mode}
        if mapping.requires_context:
            for context_name in _discover_context_names():
                ctx_with_c = {**ctx, "context_name": context_name}
                # For per-artifact templates, list every artifact
                # that exists. The ``<name>`` placeholder is
                # the snake_case filename.
                kind = _template_to_kind(mapping.template_name)
                if kind is not None:
                    for artifact in _discover_artifacts(context_name).get(
                        kind, [],
                    ):
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
                            "with_supervisor": _has_supervisor_artifact(
                                context_name,
                            ),
                        }
                        resolved.append((
                            Path(
                                mapping.rendered_path.format(
                                    package=package,
                                    context=context_name,
                                ).replace("<name>", artifact),
                            ),
                            mapping.template_name,
                            artifact_ctx,
                        ))
                else:
                    # Single-file per context (e.g. dispatcher.py).
                    resolved.append((
                        Path(
                            mapping.rendered_path.format(
                                package=package,
                                context=context_name,
                            ),
                        ),
                        mapping.template_name,
                        {
                            **ctx_with_c,
                            "with_supervisor": _has_supervisor_artifact(
                                context_name,
                            ),
                        },
                    ))
        else:
            # Project-level files (main.py, consumer.py, config.py).
            resolved.append((
                Path(mapping.rendered_path.format(package=package)),
                mapping.template_name,
                ctx,
            ))
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
        help="Relative path of the file to regenerate, e.g. "
        "src/myapp/consumer.py",
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
