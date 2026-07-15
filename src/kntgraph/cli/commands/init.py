# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
import typer
from rich.console import Console
from jinja2 import Environment, FileSystemLoader

console = Console()


def init(
    project_name: str = typer.Argument(
        ..., help="Name of the Kinetgraph project to create"
    ),
    use_intent_http: bool = typer.Option(
        False, "--use-intent-http", help="Scaffold FastAPI HTTP gateway (IntentRouter)"
    ),
):
    """
    Initialize a new Kinetgraph Modular Monolith repository.
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
        base_dir / "tests" / "unit",
        base_dir / "tests" / "integration",
    ]

    for d in dirs_to_create:
        d.mkdir(parents=True, exist_ok=True)
        # Add empty __init__.py for packages
        if "src" in d.parts:
            (d / "__init__.py").touch()

    (src_dir / "__init__.py").touch()

    # 2. Render templates
    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    context = {
        "project_name": project_name,
        "use_intent_http": use_intent_http,
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

    console.print("[green]Success![/green] Project structure created.")
