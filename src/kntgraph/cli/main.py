# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli.main -- top-level Typer for the ``knt`` CLI.

After ADR-050 (v0.10.0), every top-level command is a
*namespace* (sub-Typer): ``init project``, ``new
<artifact>``, ``keys generate``. The pre-ADR-050 flat
form ``init <name>`` is kept as a deprecated shim for
one minor cycle (removed in v0.11.0).
"""

from __future__ import annotations

import typer

from kntgraph.cli._version import version_option
from kntgraph.cli.commands import concordo, init, keys, new, upgrade

app = typer.Typer(
    name="knt",
    help="Kinetgraph CLI - Boilerplate Generator",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def main_callback(
    version: bool = version_option(),
) -> None:
    """Kinetgraph CLI."""


# Every top-level command is a sub-Typer (ADR-050 §1).
app.add_typer(init.app, name="init")
app.add_typer(new.app, name="new")
app.add_typer(keys.app, name="keys")
app.add_typer(upgrade.app, name="upgrade")
app.add_typer(concordo.app, name="concordo")


if __name__ == "__main__":
    app()
