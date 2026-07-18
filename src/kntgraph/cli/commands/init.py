# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path

import typer
from jinja2 import Environment, FileSystemLoader
from rich.console import Console

console = Console()


def init(
    project_name: str = typer.Argument(
        ..., help="Name of the Kinetgraph project to create"
    ),
    use_intent_http: bool = typer.Option(
        False,
        "--use-intent-http",
        help="Scaffold FastAPI HTTP gateway (IntentRouter)",
    ),
    routing_mode: str = typer.Option(
        "external",
        "--routing-mode",
        help="Select the intent-routing scaffold mode: external, autonomous, collaborate",
    ),
):
    """
    Initialize a new Kinetgraph Modular Monolith repository.
    """
    base_dir = Path(os.getcwd()) / project_name

    if base_dir.exists():
        console.print(f"[red]Error:[/red] Directory '{project_name}' already exists.")
        raise typer.Exit(code=1)

    routing_mode = routing_mode.lower()
    valid_modes = {"external", "autonomous", "collaborate"}
    if routing_mode not in valid_modes:
        console.print(
            f"[red]Error:[/red] Invalid routing mode '{routing_mode}'. "
            "Choose from: external, autonomous, collaborate."
        )
        raise typer.Exit(code=2)

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

    # 2. Render templates
    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    context = {
        "project_name": project_name,
        "use_intent_http": use_intent_http,
        "routing_mode": routing_mode,
    }

    # pyproject.toml
    pyproject_tmpl = env.get_template("pyproject.toml.jinja")
    (base_dir / "pyproject.toml").write_text(pyproject_tmpl.render(context))

    # .env.example
    env_tmpl = env.get_template("env.example.jinja")
    (base_dir / ".env.example").write_text(env_tmpl.render(context))

    # main.py
    main_tmpl = env.get_template("main.py.jinja")
    (src_dir / "main.py").write_text(main_tmpl.render(context))

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
            template = env.get_template(template_name)
            output_path = src_dir / relative_path
            output_path.write_text(template.render(context))

    console.print("[green]Success![/green] Project structure created.")
