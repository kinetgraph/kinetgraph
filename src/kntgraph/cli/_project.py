# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli._project -- the project's layout rules.

Every ``knt new <artifact>`` and ``knt concordo`` command runs
from the project root and addresses its target as
``<context>.<Name>``: an artifact belongs to a bounded context,
and the agent declaration derives its wiring from that
context's other artifacts.

The rules live in one place. They were duplicated in three
command modules; they are one module now.
"""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# Layout rules
# ---------------------------------------------------------------------------


def package_name() -> str:
    """The project's package -- the single directory under ``src/``.

    Exits with code 1 (after printing the reason) when the
    directory is missing or empty: every ``knt new`` command
    runs from the project root.
    """
    src_dir = Path("src")
    if not src_dir.is_dir():
        console.print(
            "[red]Error:[/red] Could not find 'src/' directory. "
            "Are you in the project root?"
        )
        raise typer.Exit(code=1)
    packages = [d for d in src_dir.iterdir() if d.is_dir() and d.name != "__pycache__"]
    if not packages:
        console.print("[red]Error:[/red] No package found inside 'src/'.")
        raise typer.Exit(code=1)
    return packages[0].name


def require_target(target: str, kind: str, example: str) -> tuple[str, str]:
    """Split a ``<context>.<Name>`` target into its two parts.

    Every ``knt new <artifact>`` and ``knt concordo`` command
    takes the context as part of the target: an artifact
    belongs to a bounded context, and the agent declaration
    derives its wiring from that context's other artifacts.

    Args:
        target: The raw target, e.g. ``"sales.CheckoutAgent"``.
        kind: The artifact kind, used in the error message.
        example: An example target, used in the error message.

    Returns:
        The context name and the artifact name.

    Raises:
        typer.Exit: With code 1 when the target carries no
            context part.
    """
    if "." not in target:
        console.print(
            f"[red]Error:[/red] Target must be in the format "
            f"<context>.<{kind}> (e.g., {example})"
        )
        raise typer.Exit(code=1)
    context_name, name = target.split(".", 1)
    return context_name, name


def snake_case(name: str) -> str:
    """CamelCase to snake_case (the CLI's naming convention)."""

    step1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", step1).lower()


def snake_to_camel(snake_str: str) -> str:
    """Convert ``snake_case`` to ``CamelCase``. The CLI's
    ``new <artifact>`` accepts CamelCase and converts to
    snake_case for the filename; the round-trip is lossy
    (CamelCase cannot be recovered from snake_case
    unambiguously). We use a simple heuristic for the
    upgrade path: split on ``_`` and capitalise each part.
    """
    parts = snake_str.split("_")
    return "".join(p.capitalize() for p in parts)


__all__ = [
    "package_name",
    "require_target",
    "snake_case",
    "snake_to_camel",
]
