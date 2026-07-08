# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
_optional — single source of truth for lazy/guarded imports
of optional dependencies.

Background
----------
The `kntgraph` package declares several third-party packages
as optional extras (``falkordb``, ``ollama``, ``gliner2``,
``fastapi``, ``litellm``, …). The framework's own modules
should be **importable** even when none of those extras are
installed, so that:

  - applications that only need the core ECS + EventLog +
    Memory can install only ``kntgraph`` (no extras) and
    still ``import kntgraph`` without errors;
  - tests can stub the optional deps via sys.meta_path
    blockers without ``ImportError`` at collection time;
  - the ``agents`` sub-module does not have to
    transitively pull in ``falkordb`` + ``ollama`` just
    because someone imports ``kntgraph.agents.roles``.

Each module that uses an optional dep uses one of two
patterns:

1. **Lazy inside a method** (preferred for runtime-only
   use, e.g. ``litellm`` in a Tool's ``invoke()``):

   .. code-block:: python

      async def invoke(self, **kwargs):
          litellm = require_optional("litellm", "kntgraph[llm]")
          ...

2. **Top-level TYPE_CHECKING-only + eager runtime import
   behind a guard** (for adapters that are importable but
   not constructible without the extra, e.g.
   ``GlinerIntentAdapter``):

   .. code-block:: python

      if TYPE_CHECKING:
          from gliner2 import GLiNER2  # noqa: F401

      class GlinerIntentAdapter:
          def __init__(self, ...):
              GLiNER2 = require_optional(
                  "gliner2", "kntgraph[gliner]",
                  purpose="GlinerIntentAdapter",
              )
              ...

The helper
----------
:func:`require_optional` raises ``ImportError`` with a
canonical message that always points the user at the
correct extra to install. Tests assert on the message
text so accidental rewording is caught.

:func:`try_import` is the non-raising variant — returns
``None`` instead. Useful for capability checks (e.g. "is
``fastapi`` available?" before exposing an HTTP route).
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Optional


def _format_message(
    package: str,
    extra: str,
    purpose: Optional[str],
) -> str:
    """
    Build the canonical ImportError message. Kept in one
    place so the wording is consistent across the
    codebase and tests can pin it.
    """
    where = purpose or "this feature"
    return (
        f"{where} requires the optional package "
        f"`{package}`, which is not installed.\n"
        f"Install it with one of:\n"
        f"    uv add {extra}\n"
        f"    pip install {extra}\n"
        f"See the `[project.optional-dependencies]` table "
        f"in the package's `pyproject.toml` for the full list "
        f"of extras."
    )


def require_optional(
    package: str,
    extra: str,
    *,
    purpose: Optional[str] = None,
) -> ModuleType:
    """
    Import ``package`` or raise ``ImportError`` with a
    message that points to the right ``extra``.

    Args:
      package: the PyPI distribution name (the string
        passed to ``importlib.import_module``).
      extra: the install extra that provides it, e.g.
        ``"kntgraph[gliner]"`` or ``"kntgraph[llm]"``.
      purpose: short human description of what was being
        attempted, for the error message. Defaults to
        "this feature".

    Returns:
      The imported module.

    Raises:
      ImportError: with the canonical message.
    """
    try:
        return importlib.import_module(package)
    except ImportError as e:
        raise ImportError(_format_message(package, extra, purpose)) from e


def try_import(
    package: str,
    extra: Optional[str] = None,
) -> Optional[ModuleType]:
    """
    Best-effort import — returns ``None`` instead of
    raising. Useful for capability checks where the
    caller wants to branch on availability rather than
    handle an exception.

    Args:
      package: the PyPI distribution name.
      extra: kept for API symmetry with
        :func:`require_optional`; not used in the return
        path because no error is raised. Provided so
        callers that switch between the two helpers do
        not have to drop the argument.

    Returns:
      The imported module, or ``None`` if not installed.
    """
    try:
        return importlib.import_module(package)
    except ImportError:
        return None


__all__ = ["require_optional", "try_import"]
