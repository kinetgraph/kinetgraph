# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli.commands.concordo -- scaffold Concordos (ADR-069 §8).

Generates a typed configuration file for a BusinessFSM or a
WorkflowSaga, plus a stub test file, so a vertical can adopt
a Concordo without hand-writing the boilerplate.

Command shape (ADR-069 §8):

  - ``knt concordo add fsm <Name> --component <Comp> --state-field <f>``
  - ``knt concordo new saga <Name> --steps a:b,c:d --timeout-ms N``
"""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.console import Console

from kntgraph.cli._templates import render_template

app = typer.Typer(help="Generate Concordos (BusinessFSM, WorkflowSaga).")
add_app = typer.Typer(help="Add a Concordo for an existing component.")
new_app = typer.Typer(help="Generate a new Concordo.")
app.add_typer(add_app, name="add")
app.add_typer(new_app, name="new")

console = Console()


def _get_package_name() -> str:
    """Naively infer the project's package name by looking inside
    the src/ directory. Assumes it is run from the project root."""
    src_dir = Path("src")
    if not src_dir.is_dir():
        console.print(
            "[red]Error:[/red] Could not find 'src/' directory. Are you in the project root?"
        )
        raise typer.Exit(code=1)
    packages = [d for d in src_dir.iterdir() if d.is_dir() and d.name != "__pycache__"]
    if not packages:
        console.print("[red]Error:[/red] No package found inside 'src/'.")
        raise typer.Exit(code=1)
    return packages[0].name


def _camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _ensure_concordos_dir(package_name: str) -> Path:
    """Create the ``concordos/`` directory (and its ``__init__.py``)
    inside the package if it does not exist."""
    target_dir = Path("src") / package_name / "concordos"
    target_dir.mkdir(parents=True, exist_ok=True)
    if not (target_dir / "__init__.py").exists():
        (target_dir / "__init__.py").touch()
    return target_dir


def _write_stub_test(package_name: str, concordo_name: str) -> Path:
    """Write a stub test file for the generated Concordo."""
    test_dir = Path("tests") / "unit" / "concordos"
    test_dir.mkdir(parents=True, exist_ok=True)
    if not (test_dir / "__init__.py").exists():
        (test_dir / "__init__.py").touch()
    snake = _camel_to_snake(concordo_name)
    test_file = test_dir / f"test_{snake}.py"
    if test_file.exists():
        return test_file
    # The SPDX header is assembled at runtime for the generated stub
    # test. The literal is wrapped in REUSE-IgnoreStart/End so the
    # REUSE linter does not mistake it for a license expression.
    # REUSE-IgnoreStart
    spdx_copyright = "# SPDX-FileCopyrightText: 2026 kinetgraph"
    spdx_license = "# SPDX-License-Identifier: Apache-2.0"
    # REUSE-IgnoreEnd
    test_file.write_text(
        f"{spdx_copyright}\n"
        f"#\n"
        f"{spdx_license}\n"
        f"\n"
        f"from {package_name}.concordos.{snake} import {concordo_name}\n"
        f"\n"
        f"\n"
        f"def test_{snake}_installs() -> None:\n"
        f"    assert {concordo_name}.name\n"
        f"    assert {concordo_name}.version\n"
    )
    return test_file


@add_app.command("fsm")
def add_fsm(
    name: str = typer.Argument(..., help="Concordo name, e.g. InvoiceFSM"),
    component: str = typer.Option(
        ...,
        "--component",
        help="DomainComponent class name, e.g. InvoiceDomainComponent",
    ),
    state_field: str = typer.Option(
        "status", "--state-field", help="State field on the component"
    ),
) -> None:
    """Generate a BusinessFSM Concordo for an existing DomainComponent."""
    package_name = _get_package_name()
    concordo_name = name if name.endswith("Concordo") else f"{name}Concordo"
    target_dir = _ensure_concordos_dir(package_name)
    snake = _camel_to_snake(concordo_name)
    target_file = target_dir / f"{snake}.py"
    if target_file.exists():
        console.print(f"[red]Error:[/red] Concordo file {target_file} already exists.")
        raise typer.Exit(code=1)
    rendered = render_template(
        "concordo_fsm.py.jinja",
        {
            "package_name": package_name,
            "concordo_name": concordo_name,
            "component_name": component,
            "state_field": state_field,
        },
    )
    target_file.write_text(rendered)
    test_file = _write_stub_test(package_name, concordo_name)
    console.print(f"[green]Success![/green] Generated FSM Concordo at {target_file}")
    console.print(f"[green]Success![/green] Generated stub test at {test_file}")


@new_app.command("saga")
def new_saga(
    name: str = typer.Argument(..., help="Saga name, e.g. NfeEmission"),
    steps: str = typer.Option(
        ...,
        "--steps",
        help="Comma-separated step:tool pairs, e.g. validate:sefaz,emit:nfe",
    ),
    timeout_ms: int = typer.Option(
        300_000, "--timeout-ms", help="Saga-level timeout in milliseconds"
    ),
) -> None:
    """Generate a WorkflowSaga Concordo."""
    package_name = _get_package_name()
    concordo_name = f"{name}Concordo"
    target_dir = _ensure_concordos_dir(package_name)
    snake = _camel_to_snake(concordo_name)
    target_file = target_dir / f"{snake}.py"
    if target_file.exists():
        console.print(f"[red]Error:[/red] Concordo file {target_file} already exists.")
        raise typer.Exit(code=1)
    parsed_steps = []
    for pair in steps.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            console.print(
                f"[red]Error:[/red] Step {pair!r} must be in the form "
                f"<step_name>:<tool_name>."
            )
            raise typer.Exit(code=1)
        step_name, tool = pair.split(":", 1)
        parsed_steps.append({"name": step_name.strip(), "tool": tool.strip()})
    rendered = render_template(
        "concordo_saga.py.jinja",
        {
            "concordo_name": concordo_name,
            "saga_name": _camel_to_snake(name),
            "timeout_ms": timeout_ms,
            "steps": parsed_steps,
        },
    )
    target_file.write_text(rendered)
    test_file = _write_stub_test(package_name, concordo_name)
    console.print(f"[green]Success![/green] Generated Saga Concordo at {target_file}")
    console.print(f"[green]Success![/green] Generated stub test at {test_file}")
