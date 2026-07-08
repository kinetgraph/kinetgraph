import os
import re
from pathlib import Path
import typer
from rich.console import Console
from jinja2 import Environment, FileSystemLoader

app = typer.Typer(help="Generate Kinetgraph artifacts (systems, events, tools, agents).")
console = Console()

def camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

def _get_package_name() -> str:
    """
    Naively infer the project's package name by looking inside the src/ directory.
    Assumes it is run from the project root.
    """
    src_dir = Path("src")
    if not src_dir.is_dir():
        console.print("[red]Error:[/red] Could not find 'src/' directory. Are you in the project root?")
        raise typer.Exit(code=1)
        
    packages = [d for d in src_dir.iterdir() if d.is_dir() and d.name != "__pycache__"]
    if not packages:
        console.print("[red]Error:[/red] No package found inside 'src/'.")
        raise typer.Exit(code=1)
        
    # Return the first package found
    return packages[0].name

@app.command()
def system(
    target: str = typer.Argument(..., help="Context and system name, e.g., sales.CheckoutSystem"),
):
    """
    Generate a pure WorldSystem artifact.
    """
    if "." not in target:
        console.print("[red]Error:[/red] Target must be in the format <context>.<SystemName> (e.g., sales.CheckoutSystem)")
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
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)
    
    tmpl = env.get_template("system.py.jinja")
    
    ctx = {
        "system_name": snake_case_name,
        "camel_case_name": system_name,
    }
    
    target_file.write_text(tmpl.render(ctx))
    console.print(f"[green]Success![/green] Generated WorldSystem at {target_file}")
