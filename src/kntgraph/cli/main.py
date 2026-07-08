import typer
from kntgraph.cli.commands import init

app = typer.Typer(
    name="knt",
    help="Kinetgraph CLI - Boilerplate Generator",
    add_completion=False,
    no_args_is_help=True,
)

@app.callback()
def main_callback():
    """Kinetgraph CLI."""
    pass

app.command(name="init")(init.init)

if __name__ == "__main__":
    app()
