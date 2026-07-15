# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import typer
from kntgraph.cli.commands import init, new, keys

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
app.add_typer(new.app, name="new")
app.add_typer(keys.app, name="keys")

if __name__ == "__main__":
    app()
