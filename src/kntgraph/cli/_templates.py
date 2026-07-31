# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli._templates -- shared Jinja machinery for the CLI.

The CLI renders the ``src/kntgraph/cli/templates/``
Jinja templates from two commands (``knt init project``
and ``knt new <artifact>``). Both paths need:

  1. A single :class:`jinja2.Environment` configured
     with the templates directory as the loader.
  2. A helper that wraps ``get_template`` + ``render``
     so the call sites stay at one line.

This module centralises both. Before ADR-050, the
``Environment`` was instantiated once per command
(``init.py:71`` and 6 sites in ``new.py``); the
helper collapses the 7 sites to 1.

The ``_ENV`` is a module-level singleton. Jinja's
``Environment`` is documented as thread-safe once
``auto_reload=False`` (the default); the CLI runs
single-threaded by design, so this is a non-issue,
but the singleton is the documented pattern.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader


_TEMPLATES_DIR = Path(__file__).parent / "templates"

# ``autoescape=False`` because the CLI renders Python
# source files, not HTML. The ``# nosec B701`` comment
# documents the deliberate choice for the bandit scan.
_ENV = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,  # nosec B701
)


def render_template(name: str, ctx: dict) -> str:
    """Render a Jinja template from the CLI templates dir.

    Args:
        name: template filename (e.g.
            ``"pyproject.toml.jinja"``). Must live in
            :data:`_TEMPLATES_DIR`.
        ctx: the Jinja context (variables exposed to
            the template).

    Returns:
        The rendered string. The caller is responsible
        for writing it to disk (this helper is
        I/O-free so it is trivially testable).
    """
    return _ENV.get_template(name).render(ctx)


__all__ = ["_TEMPLATES_DIR", "render_template"]
