# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli.commands.concordo -- scaffold Concordos (ADR-069 §8).

Generates a typed configuration file for a BusinessFSM or a
WorkflowSaga, plus a stub test file, so a vertical can adopt
a Concordo without hand-writing the boilerplate.

A Concordo belongs to a **bounded context**: it is the
context's behaviour, and the agent declaration in that context
references it. The command therefore takes the context as part
of the target, exactly like ``knt new <artifact>`` does.

Command shape (ADR-069 §8, ADR-088 §2.5):

  - ``knt concordo add fsm <ctx>.<Name> --component <Comp> --state-field <f>``
  - ``knt concordo new saga <ctx>.<Name> --steps a:b,c:d --timeout-ms N``
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from kntgraph.cli._derive import refresh_derived_files
from kntgraph.cli._project import (
    package_name,
    require_target,
    snake_case,
)
from kntgraph.cli._scanner import (
    CONCORDOS_DIR,
    scan_context,
)
from kntgraph.cli._templates import render_template
from kntgraph.core._typing import JsonValue

app = typer.Typer(help="Generate Concordos (BusinessFSM, WorkflowSaga).")
add_app = typer.Typer(help="Add a Concordo for an existing component.")
new_app = typer.Typer(help="Generate a new Concordo.")
app.add_typer(add_app, name="add")
app.add_typer(new_app, name="new")

console = Console()


def _ensure_concordos_dir(package: str, context: str) -> Path:
    """Create the context's ``concordos/`` directory (and its
    ``__init__.py``) if it does not exist."""
    target_dir = Path("src") / package / "contexts" / context / CONCORDOS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    if not (target_dir / "__init__.py").exists():
        (target_dir / "__init__.py").touch()
    return target_dir


def _write_stub_test(
    package: str,
    context: str,
    concordo_name: str,
) -> Path:
    """Write a stub test file for the generated Concordo."""
    test_dir = Path("tests") / "unit" / "concordos"
    test_dir.mkdir(parents=True, exist_ok=True)
    if not (test_dir / "__init__.py").exists():
        (test_dir / "__init__.py").touch()
    snake = snake_case(concordo_name)
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
        f"from {package}.contexts.{context}.concordos.{snake} import "
        f"{concordo_name}\n"
        f"\n"
        f"\n"
        f"def test_{snake}_installs() -> None:\n"
        f"    assert {concordo_name}.name\n"
        f"    assert {concordo_name}.systems\n"
    )
    return test_file


def _resolve_component(
    package: str,
    context: str,
    component_name: str,
) -> str:
    """The import path of the domain component the FSM reads.

    The component lives in the context's ``components/``
    directory. The scanner finds the module that declares the
    class; the snake_case fallback covers a component the user
    has not scaffolded yet.

    Args:
        package: The project package.
        context: The bounded context.
        component_name: The component's class name.

    Returns:
        The dotted module path relative to the context package.
    """
    inventory = scan_context(package, context)
    for component in inventory.components:
        if component.class_name == component_name:
            return f"..{component.module}"
    return f"..components.{snake_case(component_name)}"


@add_app.command("fsm")
def add_fsm(
    target: str = typer.Argument(
        ..., help="Context and Concordo name, e.g. sales.InvoiceFSM"
    ),
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
    context_name, name = require_target(target, "ConcordoName", "sales.InvoiceFSM")
    package = package_name()
    concordo_name = name if name.endswith("Concordo") else f"{name}Concordo"
    target_dir = _ensure_concordos_dir(package, context_name)
    snake = snake_case(concordo_name)
    target_file = target_dir / f"{snake}.py"
    if target_file.exists():
        console.print(f"[red]Error:[/red] Concordo file {target_file} already exists.")
        raise typer.Exit(code=1)
    fsm_ctx: dict[str, JsonValue] = {
        "package_name": package,
        "context_name": context_name,
        "concordo_name": concordo_name,
        "component_name": component,
        "component_module": _resolve_component(package, context_name, component),
        "state_field": state_field,
    }
    rendered = render_template("concordo_fsm.py.jinja", fsm_ctx)
    target_file.write_text(rendered)
    test_file = _write_stub_test(package, context_name, concordo_name)
    console.print(f"[green]Success![/green] Generated FSM Concordo at {target_file}")
    console.print(f"[green]Success![/green] Generated stub test at {test_file}")
    _refresh_context(package, context_name, f"{CONCORDOS_DIR}.{snake}")


@new_app.command("saga")
def new_saga(
    target: str = typer.Argument(
        ..., help="Context and Saga name, e.g. sales.NfeEmission"
    ),
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
    context_name, name = require_target(target, "SagaName", "sales.NfeEmission")
    package = package_name()
    concordo_name = f"{name}Concordo"
    target_dir = _ensure_concordos_dir(package, context_name)
    snake = snake_case(concordo_name)
    target_file = target_dir / f"{snake}.py"
    if target_file.exists():
        console.print(f"[red]Error:[/red] Concordo file {target_file} already exists.")
        raise typer.Exit(code=1)
    parsed_steps: list[JsonValue] = []
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
    saga_ctx: dict[str, JsonValue] = {
        "package_name": package,
        "context_name": context_name,
        "concordo_name": concordo_name,
        "saga_name": name,
        "steps": parsed_steps,
        "timeout_ms": timeout_ms,
    }
    rendered = render_template("concordo_saga.py.jinja", saga_ctx)
    target_file.write_text(rendered)
    test_file = _write_stub_test(package, context_name, concordo_name)
    console.print(f"[green]Success![/green] Generated Saga Concordo at {target_file}")
    console.print(f"[green]Success![/green] Generated stub test at {test_file}")
    _refresh_context(package, context_name, f"{CONCORDOS_DIR}.{snake}")


def _refresh_context(package: str, context: str, added_module: str) -> None:
    """Refresh the context's derived files after a Concordo was added.

    The agent declaration references the Concordos, so it is a
    derived file too. A declaration the user has edited is left
    alone; the CLI says so instead of clobbering it.
    """
    rewritten, skipped = refresh_derived_files(
        package, context, exclude_module=added_module
    )
    for path in rewritten:
        console.print(f"[green]Success![/green] Regenerated {path}")
    for path in skipped:
        console.print(
            f"[yellow]Hint:[/yellow] {path} was edited by hand, so it was "
            f"not regenerated. Run 'knt upgrade apply {path} --force' to "
            f"overwrite it."
        )


__all__ = ["add_fsm", "app", "new_saga"]
