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

Two failures, two remedies
---------------------------
A package can be missing, or it can be present and still
unable to run — the second case is the *inference runtime*
of an ML package (GLiNER2 needs ``torch``; ``torch`` is not
a dependency of ``gliner2`` itself, it lives in the
``local`` extra of that package). Those two failures need
two different messages: pointing the operator at the
framework's extra in the second case sends them in a
circle, because installing the extra again reproduces the
same error. :func:`require_optional` therefore takes a
``runtime_hint`` and uses it when the package is present
but its own import failed.

:func:`try_import` is the non-raising variant — returns
``None`` instead. Useful for capability checks (e.g. "is
``fastapi`` available?" before exposing an HTTP route).
"""

from __future__ import annotations

import importlib
from types import ModuleType

from kntgraph._optional_messages import message_for


def require_optional(
    package: str,
    extra: str,
    *,
    purpose: str | None = None,
    runtime_hint: str | None = None,
) -> ModuleType:
    """
    Import ``package`` or raise ``ImportError`` with a
    message that points to the right remedy.

    Two failures are distinguished, because they have
    different remedies:

    1. **The package is not installed.** The message points
       at the framework's extra.

    2. **The package is installed but cannot run** — one of
       its own dependencies is missing (the *inference
       runtime*). Installing the framework's extra again
       would reproduce the same error, so the message names
       the missing module and shows ``runtime_hint`` — the
       choice the application actually has: install the
       runtime it wants, or inject a model it already has
       and skip the runtime entirely.

       This case is only reported when ``runtime_hint`` is
       given; without it, the message falls back to the
       canonical one.

    Args:
      package: the PyPI distribution name (the string
        passed to ``importlib.import_module``).
      extra: the install extra that provides it, e.g.
        ``"kntgraph[gliner]"`` or ``"kntgraph[llm]"``.
      purpose: short human description of what was being
        attempted, for the error message. Defaults to
        "this feature".
      runtime_hint: what to do when the package is present
        but its runtime is missing. Omit it when the
        package has no runtime of its own.

    Returns:
      The imported module.

    Raises:
      ImportError: with the canonical message.
    """
    try:
        return importlib.import_module(package)
    except ImportError as e:
        raise ImportError(message_for(e, package, extra, purpose, runtime_hint)) from e


def try_import(
    package: str,
    extra: str | None = None,
) -> ModuleType | None:
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
