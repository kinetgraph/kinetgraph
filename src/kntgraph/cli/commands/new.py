# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import typer
from jinja2 import Environment, FileSystemLoader
from rich.console import Console

from kntgraph.cli._derive import (
    agent_context,
    dispatcher_context,
    refresh_derived_files,
)
from kntgraph.cli._project import (
    package_name,
    require_target,
    snake_case,
)
from kntgraph.cli._scanner import (
    AGENTS_DIR,
    CONCORDOS_DIR,
    SWEEPS_DIR,
    TOOLS_DIR,
    ContextInventory,
    scan_context,
)

app = typer.Typer(
    help="Generate Kinetgraph artifacts (systems, events, tools, agents)."
)
console = Console()


@app.command()
def system(
    target: str = typer.Argument(
        ..., help="Context and system name, e.g., sales.CheckoutSystem"
    ),
):
    """
    Generate a pure WorldSystem artifact.
    """
    context_name, system_name = require_target(
        target, "SystemName", "sales.CheckoutSystem"
    )
    package = package_name()

    snake_case_name = snake_case(system_name)
    if not snake_case_name.endswith("_system"):
        snake_case_name += "_system"

    target_dir = Path("src") / package / "contexts" / context_name / "systems"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Create __init__.py for new dirs if they don't exist
    for p in [target_dir, target_dir.parent]:
        if not (p / "__init__.py").exists():
            (p / "__init__.py").touch()

    target_file = target_dir / f"{snake_case_name}.py"

    if target_file.exists():
        console.print(f"[red]Error:[/red] System file {target_file} already exists.")
        raise typer.Exit(code=1)

    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    tmpl = env.get_template("system.py.jinja")

    ctx = {
        "system_name": snake_case_name,
        "camel_case_name": system_name,
    }

    target_file.write_text(tmpl.render(ctx))
    console.print(f"[green]Success![/green] Generated WorldSystem at {target_file}")


@app.command()
def component(
    target: str = typer.Argument(
        ..., help="Context and component name, e.g., sales.CartItem"
    ),
):
    """
    Generate an immutable ECS Component artifact.
    """
    context_name, component_name = require_target(
        target, "ComponentName", "sales.CartItem"
    )
    package = package_name()

    snake_case_name = snake_case(component_name)

    target_dir = Path("src") / package / "contexts" / context_name / "components"
    target_dir.mkdir(parents=True, exist_ok=True)

    for p in [target_dir, target_dir.parent]:
        if not (p / "__init__.py").exists():
            (p / "__init__.py").touch()

    target_file = target_dir / f"{snake_case_name}.py"

    if target_file.exists():
        console.print(f"[red]Error:[/red] Component file {target_file} already exists.")
        raise typer.Exit(code=1)

    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    tmpl = env.get_template("component.py.jinja")

    ctx = {
        "camel_case_name": component_name,
    }

    target_file.write_text(tmpl.render(ctx))
    console.print(f"[green]Success![/green] Generated Component at {target_file}")


@app.command()
def event(
    target: str = typer.Argument(
        ..., help="Context and event name, e.g., sales.OrderPlaced"
    ),
):
    """
    Generate a domain Event factory artifact.
    """
    context_name, event_name = require_target(target, "EventName", "sales.OrderPlaced")
    package = package_name()

    snake_case_name = snake_case(event_name)

    target_dir = Path("src") / package / "contexts" / context_name / "events"
    target_dir.mkdir(parents=True, exist_ok=True)

    for p in [target_dir, target_dir.parent]:
        if not (p / "__init__.py").exists():
            (p / "__init__.py").touch()

    target_file = target_dir / f"{snake_case_name}.py"

    if target_file.exists():
        console.print(f"[red]Error:[/red] Event file {target_file} already exists.")
        raise typer.Exit(code=1)

    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    tmpl = env.get_template("event.py.jinja")

    ctx = {
        "event_name": snake_case_name,
        "camel_case_name": event_name,
        "event_type": f"{context_name}.{snake_case_name}",
    }

    target_file.write_text(tmpl.render(ctx))
    console.print(f"[green]Success![/green] Generated Event factory at {target_file}")


@app.command()
def tool(
    target: str = typer.Argument(
        ..., help="Context and tool name, e.g., sales.ProcessPayment"
    ),
):
    """
    Generate an impure Tool artifact.
    """
    context_name, tool_name = require_target(target, "ToolName", "sales.ProcessPayment")
    package = package_name()

    snake_case_name = snake_case(tool_name)

    target_dir = Path("src") / package / "contexts" / context_name / "tools"
    target_dir.mkdir(parents=True, exist_ok=True)

    for p in [target_dir, target_dir.parent]:
        if not (p / "__init__.py").exists():
            (p / "__init__.py").touch()

    target_file = target_dir / f"{snake_case_name}.py"

    if target_file.exists():
        console.print(f"[red]Error:[/red] Tool file {target_file} already exists.")
        raise typer.Exit(code=1)

    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    tmpl = env.get_template("tool.py.jinja")

    ctx = {
        "tool_name": snake_case_name,
        "camel_case_name": tool_name,
    }

    target_file.write_text(tmpl.render(ctx))
    console.print(f"[green]Success![/green] Generated Tool at {target_file}")
    _refresh_context(package, context_name, f"{TOOLS_DIR}.{snake_case_name}")


@app.command()
def sweep(
    target: str = typer.Argument(
        ..., help="Context and sweep name, e.g., sales.CompactionSweep"
    ),
):
    """
    Generate a cyclic sweep (the Runner's work).

    A sweep is a ``WorldSystem`` that runs on a **timer**
    rather than on an event: consolidation, compaction,
    reconciliation. The ``ReactiveDispatcher`` runs the
    event-driven systems per agent; the ``Runner`` runs the
    sweeps over the whole World.

    The sweep is *named*, not inferred: the context's
    dispatcher registers it explicitly.
    """
    context_name, sweep_name = require_target(
        target, "SweepName", "sales.CompactionSweep"
    )
    package = package_name()

    snake_case_name = snake_case(sweep_name)
    if not snake_case_name.endswith("_sweep"):
        snake_case_name += "_sweep"

    target_dir = Path("src") / package / "contexts" / context_name / SWEEPS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    for p in [target_dir, target_dir.parent]:
        if not (p / "__init__.py").exists():
            (p / "__init__.py").touch()

    target_file = target_dir / f"{snake_case_name}.py"

    if target_file.exists():
        console.print(f"[red]Error:[/red] Sweep file {target_file} already exists.")
        raise typer.Exit(code=1)

    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    tmpl = env.get_template("sweep.py.jinja")

    ctx = {
        "sweep_name": sweep_name,
        "function_name": snake_case_name,
    }

    target_file.write_text(tmpl.render(ctx))
    console.print(f"[green]Success![/green] Generated Sweep at {target_file}")
    _refresh_context(package, context_name, f"{SWEEPS_DIR}.{snake_case_name}")


@app.command()
def agent(
    target: str = typer.Argument(
        ..., help="Context and agent name, e.g., sales.CheckoutAgent"
    ),
):
    """
    Generate an agent declaration (ADR-088).

    The generated file is one ``AgentDefinition``: the persona,
    the capability policy, the state machine and the workflows.
    Every part is **derived from the context** -- the tool
    allow-list is the context's tools, the domain types are its
    components, and the two execution cycles are its Concordos.

    The runtime is derived from the declaration
    (``definition.bundle()``); the dispatcher installs it with
    ``definition.install(dispatcher, principal=principal)``.
    """
    context_name, agent_name = require_target(
        target, "AgentName", "sales.CheckoutAgent"
    )
    package = package_name()

    snake_case_name = snake_case(agent_name)
    if not snake_case_name.endswith("_agent"):
        snake_case_name += "_agent"

    target_dir = Path("src") / package / "contexts" / context_name / "agents"
    target_dir.mkdir(parents=True, exist_ok=True)

    for p in [target_dir, target_dir.parent]:
        if not (p / "__init__.py").exists():
            (p / "__init__.py").touch()

    target_file = target_dir / f"{snake_case_name}.py"

    if target_file.exists():
        console.print(f"[red]Error:[/red] Agent file {target_file} already exists.")
        raise typer.Exit(code=1)

    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    tmpl = env.get_template("agent.py.jinja")

    # The declaration's name drops the ``_agent`` suffix: the
    # suffix describes the file, not the agent.
    declaration_name = snake_case_name.removesuffix("_agent")
    inventory = scan_context(package, context_name)
    ctx = agent_context(
        inventory,
        agent_name=declaration_name,
        persona=declaration_name,
    )

    target_file.write_text(tmpl.render(ctx))
    console.print(f"[green]Success![/green] Generated Agent at {target_file}")
    _refresh_context(package, context_name, f"{AGENTS_DIR}.{snake_case_name}")
    _print_declaration_hints(inventory)


def _print_declaration_hints(inventory: ContextInventory) -> None:
    """Print what the declaration is missing.

    An empty allow-list forbids every tool, and an agent with
    no state machine has no reactive cycle -- both are worth
    saying out loud at the moment the declaration is written.
    """
    if not inventory.tools:
        console.print(
            "[yellow]Hint:[/yellow] The context declares no tool yet, so "
            "the agent's allow-list is empty -- an empty allow-list "
            "forbids every tool. Run 'knt new tool <context>.<ToolName>' "
            "and add the tool name to the allow-list."
        )
    if inventory.fsm() is None:
        console.print(
            "[yellow]Hint:[/yellow] The context declares no FSM, so the "
            "agent has no reactive cycle. Run 'knt concordo add fsm "
            "<context>.<Name> --component <Component>'."
        )


def _refresh_context(package: str, context: str, added_module: str) -> None:
    """Refresh the context's derived files after an artifact was added.

    A derived file the user has edited is left alone; the CLI
    says so instead of clobbering hand-written wiring.
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


@app.command()
def context(
    target: str = typer.Argument(..., help="Context name, e.g., sales"),
):
    """
    Generate a Bounded Context directory structure and its ReactiveDispatcher.
    """
    context_name = snake_case(target)
    package = package_name()

    target_dir = Path("src") / package / "contexts" / context_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories. The concordos join them: they are
    # the context's two execution cycles, and the agent
    # declaration references them.
    subdirs = [
        "agents",
        "components",
        CONCORDOS_DIR,
        "events",
        SWEEPS_DIR,
        "systems",
        "tools",
    ]
    for subdir in subdirs:
        sub_path = target_dir / subdir
        sub_path.mkdir(exist_ok=True)
        if not (sub_path / "__init__.py").exists():
            (sub_path / "__init__.py").touch()

    if not (target_dir / "__init__.py").exists():
        (target_dir / "__init__.py").touch()

    dispatcher_file = target_dir / "dispatcher.py"

    if dispatcher_file.exists():
        console.print(
            f"[red]Error:[/red] Dispatcher file {dispatcher_file} already exists."
        )
        raise typer.Exit(code=1)

    templates_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # nosec B701

    tmpl = env.get_template("dispatcher.py.jinja")

    # The dispatcher registers the context's tools and installs
    # the context's agent declarations. Both are derived from
    # what the context already holds.
    ctx = dispatcher_context(
        scan_context(package, context_name), context_name=context_name
    )

    dispatcher_file.write_text(tmpl.render(ctx))
    console.print(
        f"[green]Success![/green] Generated Bounded Context "
        f"'{context_name}' at {target_dir}"
    )
