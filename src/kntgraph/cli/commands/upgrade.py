# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli.commands.upgrade -- ``knt upgrade`` (ADR-053).

The ``knt new <artifact>`` commands render Jinja templates
into ``src/<package>/contexts/<context>/...`` files. When the
framework evolves, the boilerplate on disk goes stale: the user
has to either ``git rm`` the old file and re-run ``knt new``
(losing local edits) or hand-merge the diff (error-prone).

``knt upgrade`` is the canonical entry point for the
**post-init regeneration** workflow. It supports three
operating modes:

  - ``knt upgrade check``: report which generated boilerplate
    files are out of date **without** writing anything. Exit
    code 0 when no drift is detected, 1 when drift is found.
    Use this in CI to detect bots that edited boilerplate
    without running the upgrade.

  - ``knt upgrade apply <relative_path>``: regenerate a
    single file. The relative path is from the project root,
    e.g. ``src/myapp/consumer.py``. The command renders the
    current template with the discovered context and writes
    the result. **Local edits are lost** unless the template
    supports a merge mode (currently none).

  - ``knt upgrade apply-all --force``: regenerate every
    boilerplate file that has a corresponding template. The
    ``--force`` flag is mandatory because the operation
    overwrites without diff confirmation.

The knowledge of *which* files are boilerplate lives in
``kntgraph.cli._boilerplate``.

The CLI does NOT touch user-modified application code. The
template renderer is purely substitutional and never inspects
the user's logical changes -- it only knows the template
contract.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from kntgraph.cli._boilerplate import (
    render_boilerplate,
    resolve_mappings,
)

app = typer.Typer(
    help="Regenerate boilerplate files against the current templates.",
)
console = Console()


# ------------------------------------------------------------------
# CLI surface
# ------------------------------------------------------------------


@app.command()
def list_templates() -> None:
    """Print the resolved list of boilerplate mappings.

    For each (template_name, rendered_path) the command
    renders the current template with the discovered context
    and prints the resulting *target* path. Useful for
    debugging the upgrade machinery.
    """
    rows = resolve_mappings()
    if not rows:
        console.print(
            "[red]Error:[/red] No boilerplate found. Are you "
            "in a Kinetgraph project root?",
        )
        raise typer.Exit(code=1)
    table = Table("Target path", "Template", title="Boilerplate")
    for path, template_name, _ctx in rows:
        table.add_row(str(path), template_name)
    console.print(table)


@app.command()
def check() -> None:
    """Report which generated boilerplate files are out of date.

    Exit code 0 when no drift is detected, 1 when drift is
    found. ``--quiet`` suppresses the per-file output (only
    the summary is printed); useful in CI.
    """
    rows = resolve_mappings()
    if not rows:
        console.print(
            "[red]Error:[/red] No boilerplate found. Are you "
            "in a Kinetgraph project root?",
        )
        raise typer.Exit(code=1)

    drifted: list[tuple[Path, str, str]] = []
    for path, template_name, ctx in rows:
        if not path.exists():
            drifted.append((path, template_name, "missing"))
            continue
        current = path.read_text(encoding="utf-8")
        expected = render_boilerplate(template_name, ctx)
        if current != expected:
            drifted.append((path, template_name, "drifted"))

    if drifted:
        table = Table("Path", "Template", "Status", title="Drift detected")
        for path, template_name, status in drifted:
            table.add_row(str(path), template_name, status)
        console.print(table)
        console.print(
            f"[yellow]{len(drifted)} file(s) drifted. "
            f"Run 'knt upgrade apply-all' to regenerate.[/yellow]",
        )
        raise typer.Exit(code=1)
    console.print("[green]No drift detected.[/green]")


@app.command()
def apply(
    target: str = typer.Argument(
        ...,
        help="Relative path of the file to regenerate, e.g. src/myapp/consumer.py",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite the file even if it has local modifications.",
    ),
) -> None:
    """Regenerate a single boilerplate file."""
    rows = resolve_mappings()
    target_path = Path(target)
    match = next(
        (r for r in rows if r[0] == target_path),
        None,
    )
    if match is None:
        console.print(
            f"[red]Error:[/red] {target!r} is not a known "
            f"boilerplate file. Run 'knt upgrade list-templates'.",
        )
        raise typer.Exit(code=1)

    path, template_name, ctx = match
    expected = render_boilerplate(template_name, ctx)

    if not force and path.exists():
        current = path.read_text(encoding="utf-8")
        if current != expected:
            console.print(
                f"[red]Error:[/red] {path} has local modifications. "
                f"Re-run with --force to overwrite.",
            )
            raise typer.Exit(code=1)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8")
    console.print(f"[green]Regenerated[/green] {path}")


@app.command()
def apply_all(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite files even if they have local modifications.",
    ),
) -> None:
    """Regenerate every boilerplate file."""
    rows = resolve_mappings()
    if not rows:
        console.print(
            "[red]Error:[/red] No boilerplate found. Are you "
            "in a Kinetgraph project root?",
        )
        raise typer.Exit(code=1)

    regenerated = 0
    skipped = 0
    for path, template_name, ctx in rows:
        expected = render_boilerplate(template_name, ctx)
        if path.exists():
            current = path.read_text(encoding="utf-8")
            if current != expected and not force:
                console.print(
                    f"[yellow]Skip[/yellow] {path} (local modifications; "
                    f"pass --force to overwrite)",
                )
                skipped += 1
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        console.print(f"[green]Regenerated[/green] {path}")
        regenerated += 1
    console.print(
        f"Done: {regenerated} regenerated, {skipped} skipped.",
    )


__all__ = ["app", "apply", "apply_all", "check", "list_templates"]
