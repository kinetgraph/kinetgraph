import re
from pathlib import Path
import typer
from rich.console import Console
from jinja2 import Environment, FileSystemLoader

app = typer.Typer(
    help="Generate Kinetgraph artifacts (systems, events, tools, agents)."
)
console = Console()


def camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _get_package_name() -> str:
    """
    Naively infer the project's package name by looking inside the src/ directory.
    Assumes it is run from the project root.
    """
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

    # Return the first package found
    return packages[0].name


@app.command()
def system(
    target: str = typer.Argument(
        ..., help="Context and system name, e.g., sales.CheckoutSystem"
    ),
):
    """
    Generate a pure WorldSystem artifact.
    """
    if "." not in target:
        console.print(
            "[red]Error:[/red] Target must be in the format <context>.<SystemName> (e.g., sales.CheckoutSystem)"
        )
        raise typer.Exit(code=1)

    context_name, system_name = target.split(".", 1)
    package_name = _get_package_name()

    snake_case_name = camel_to_snake(system_name)
    if not snake_case_name.endswith("_system"):
        snake_case_name += "_system"

    target_dir = Path("src") / package_name / "contexts" / context_name / "systems"
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
    if "." not in target:
        console.print(
            "[red]Error:[/red] Target must be in the format <context>.<ComponentName> (e.g., sales.CartItem)"
        )
        raise typer.Exit(code=1)

    context_name, component_name = target.split(".", 1)
    package_name = _get_package_name()

    snake_case_name = camel_to_snake(component_name)

    target_dir = Path("src") / package_name / "contexts" / context_name / "components"
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
    if "." not in target:
        console.print(
            "[red]Error:[/red] Target must be in the format <context>.<EventName> (e.g., sales.OrderPlaced)"
        )
        raise typer.Exit(code=1)

    context_name, event_name = target.split(".", 1)
    package_name = _get_package_name()

    snake_case_name = camel_to_snake(event_name)

    target_dir = Path("src") / package_name / "contexts" / context_name / "events"
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
    if "." not in target:
        console.print(
            "[red]Error:[/red] Target must be in the format <context>.<ToolName> (e.g., sales.ProcessPayment)"
        )
        raise typer.Exit(code=1)

    context_name, tool_name = target.split(".", 1)
    package_name = _get_package_name()

    snake_case_name = camel_to_snake(tool_name)

    target_dir = Path("src") / package_name / "contexts" / context_name / "tools"
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


@app.command()
def agent(
    target: str = typer.Argument(
        ..., help="Context and agent name, e.g., sales.CheckoutAgent"
    ),
):
    """
    Generate an Agent orchestration file (wiring Systems and Tools).
    """
    if "." not in target:
        console.print(
            "[red]Error:[/red] Target must be in the format <context>.<AgentName> (e.g., sales.CheckoutAgent)"
        )
        raise typer.Exit(code=1)

    context_name, agent_name = target.split(".", 1)
    package_name = _get_package_name()

    snake_case_name = camel_to_snake(agent_name)
    if not snake_case_name.endswith("_agent"):
        snake_case_name += "_agent"

    target_dir = Path("src") / package_name / "contexts" / context_name / "agents"
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

    ctx = {
        "agent_name": snake_case_name,
        "camel_case_name": agent_name,
        "context_name": context_name,
    }

    target_file.write_text(tmpl.render(ctx))
    console.print(f"[green]Success![/green] Generated Agent at {target_file}")


@app.command()
def context(
    target: str = typer.Argument(..., help="Context name, e.g., sales"),
):
    """
    Generate a Bounded Context directory structure and its ReactiveDispatcher.
    """
    context_name = camel_to_snake(target)
    package_name = _get_package_name()

    target_dir = Path("src") / package_name / "contexts" / context_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    subdirs = ["agents", "components", "events", "systems", "tools"]
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

    ctx = {
        "context_name": context_name,
    }

    dispatcher_file.write_text(tmpl.render(ctx))
    console.print(
        f"[green]Success![/green] Generated Bounded Context '{context_name}' at {target_dir}"
    )
