# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli.commands.init -- ``knt init project <name>`` (ADR-050).

After the v0.10.0 cleanup, ``init`` is a sub-Typer with
a single ``project`` sub-command. The pre-ADR-050 flat
form (``knt init <name>``) still works for one minor
cycle (deprecation warning printed) and is removed in
v0.11.0.
"""

from __future__ import annotations

import os
import warnings
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console

from kntgraph.cli._templates import render_template


console = Console()


# ADR-050 §2: declarative enum for ``--routing-mode``.
# Replaces the imperative ``valid_modes`` set + the
# hand-written error message. Typer auto-lists the
# valid choices in ``--help`` and rejects unknown
# values with the standard ``Invalid value`` error.
class RoutingMode(str, Enum):
    """The canonical intent-routing scaffold modes.

    Values match the file names in
    ``templates/routing/adapters/`` (one module per
    mode).
    """

    external = "external"
    autonomous = "autonomous"
    collaborate = "collaborate"


# Module-level Typer (sub-Typer under ``knt``). The
# entry point is the ``project`` sub-command.
app = typer.Typer(
    help="Initialize a new Kinetgraph Modular Monolith repository.",
)


def _do_init(
    project_name: str,
    use_intent_http: bool,
    routing_mode: RoutingMode,
) -> None:
    """The actual scaffold logic. Shared between the
    sub-Typer entry point and the deprecated flat-form
    shim (so the two surfaces cannot drift).
    """
    base_dir = Path(os.getcwd()) / project_name

    if base_dir.exists():
        console.print(f"[red]Error:[/red] Directory '{project_name}' already exists.")
        raise typer.Exit(code=1)

    console.print(f"Initializing Kinetgraph project: {project_name}")

    # 1. Create directories
    src_dir = base_dir / "src" / project_name
    dirs_to_create = [
        src_dir / "core",
        src_dir / "contexts",
        src_dir / "routing" / "adapters",
        base_dir / "tests" / "unit",
        base_dir / "tests" / "integration",
    ]

    for directory in dirs_to_create:
        directory.mkdir(parents=True, exist_ok=True)
        if "src" in directory.parts:
            (directory / "__init__.py").touch()

    (src_dir / "__init__.py").touch()
    (src_dir / "routing" / "__init__.py").touch()
    (src_dir / "routing" / "adapters" / "__init__.py").touch()

    # 2. Render templates (ADR-050 §3: shared helper).
    context = {
        "project_name": project_name,
        "use_intent_http": use_intent_http,
        "routing_mode": routing_mode.value,
    }

    (base_dir / "pyproject.toml").write_text(
        render_template("pyproject.toml.jinja", context)
    )
    (base_dir / ".env.example").write_text(
        render_template("env.example.jinja", context)
    )
    (src_dir / "main.py").write_text(render_template("main.py.jinja", context))

    # routing modules
    if not use_intent_http:
        routing_files = {
            "routing/__init__.py": "routing/__init__.py.jinja",
            "routing/components.py": "routing/components.py.jinja",
            "routing/policy.py": "routing/policy.py.jinja",
            "routing/resolution.py": "routing/resolution.py.jinja",
            "routing/adapters/external.py": "routing/adapters/external.py.jinja",
            "routing/adapters/autonomous.py": "routing/adapters/autonomous.py.jinja",
            "routing/adapters/collaborate.py": "routing/adapters/collaborate.py.jinja",
            "routing/coordinator.py": "routing/coordinator.py.jinja",
        }
        for relative_path, template_name in routing_files.items():
            (src_dir / relative_path).write_text(
                render_template(template_name, context)
            )

    console.print("[green]Success![/green] Project structure created.")


@app.command(name="project")
def project(
    project_name: str = typer.Argument(
        ..., help="Name of the Kinetgraph project to create"
    ),
    use_intent_http: bool = typer.Option(
        False,
        "--use-intent-http",
        help="Scaffold FastAPI HTTP gateway (IntentRouter)",
    ),
    routing_mode: RoutingMode = typer.Option(
        RoutingMode.external,
        "--routing-mode",
        case_sensitive=False,
        help="Select the intent-routing scaffold mode.",
    ),
) -> None:
    """Initialize a new Kinetgraph Modular Monolith repository."""
    _do_init(
        project_name=project_name,
        use_intent_http=use_intent_http,
        routing_mode=routing_mode,
    )


# ---------------------------------------------------------------------------
# Deprecated flat form (ADR-050 §3 "Deprecation policy").
#
# ``knt init <name>`` (without the ``project`` sub-command)
# still works in v0.10.0 and prints a DeprecationWarning
# pointing operators at the new form. The flat form is
# removed in v0.11.0 (one minor cycle window per
# AGENTS.md §2).
#
# Implemented as a standalone function that the parent
# ``main.py`` registers as a sub-command named ``init``
# (no, that would collide with the sub-Typer — see
# main.py for the bridge).
# ---------------------------------------------------------------------------


def deprecated_flat_init(
    ctx: typer.Context,
    project_name: str = typer.Argument(
        ..., help="Name of the Kinetgraph project to create"
    ),
    use_intent_http: bool = typer.Option(False, "--use-intent-http", hidden=True),
    routing_mode: RoutingMode = typer.Option(
        RoutingMode.external,
        "--routing-mode",
        case_sensitive=False,
        hidden=True,
    ),
) -> None:
    """Deprecated alias for ``knt init project``.

    Prints a DeprecationWarning and delegates to
    :func:`_do_init`. Scheduled for removal in
    v0.11.0.
    """
    warnings.warn(
        "`knt init <name>` is deprecated; use "
        "`knt init project <name>` instead. "
        "The flat form is removed in v0.11.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    _do_init(
        project_name=project_name,
        use_intent_http=use_intent_http,
        routing_mode=routing_mode,
    )


__all__ = [
    "RoutingMode",
    "app",
    "deprecated_flat_init",
    "project",
]
