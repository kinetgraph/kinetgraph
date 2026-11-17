# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli._version -- the ``knt --version`` flag.

The installer verifies its own work by asking the CLI what
version it installed, so the flag has to answer before any
command runs.

The callback lives in its own module so the top-level Typer
app stays a wiring surface: the CLI's entry point is a list of
sub-Typers and one option.
"""

from __future__ import annotations

import typer


def version_callback(value: bool) -> None:
    """Print the installed version and exit.

    Args:
        value: Whether the flag was given.
    """
    if not value:
        return
    from kntgraph import __version__

    typer.echo(f"knt {__version__}")
    raise typer.Exit()


def version_option() -> typer.Option:
    """The ``--version`` option, ready to hang off a callback.

    Returns:
        The Typer option. It is eager, so it answers before
        any sub-command runs.
    """
    return typer.Option(
        False,
        "--version",
        "-V",
        callback=version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    )


__all__ = ["version_callback", "version_option"]
