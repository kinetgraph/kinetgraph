# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
#

"""
_optional_messages — the words of the optional-dependency guard.

Kept apart from :mod:`kntgraph._optional` so the guard
itself stays a thin seam (one import, one ``try``), and so
the wording of the two failures lives in one place.

Two failures, two remedies
---------------------------
A package can be **missing**, or it can be **present and
unable to run** — the second case is the inference runtime
of an ML package (GLiNER2 needs ``torch``; ``torch`` is not
a dependency of ``gliner2`` itself, it lives in the ``local``
extra of that package).

Those need two different messages. Pointing the operator at
the framework's extra in the second case sends them in a
circle: installing the extra again reproduces the same error.
"""

from __future__ import annotations


def format_missing_package(
    package: str,
    extra: str,
    purpose: str | None,
) -> str:
    """
    The message for "the package is not installed".

    The remedy is the framework's extra.
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


def format_missing_runtime(
    package: str,
    missing: str,
    purpose: str | None,
    runtime_hint: str,
) -> str:
    """
    The message for "the package is installed, its runtime is
    not".

    The remedy is the runtime the application wants — or the
    model the application already has. The hint says so.
    """
    where = purpose or "this feature"
    return (
        f"{where} requires the optional package "
        f"`{package}`, which is installed but cannot run: "
        f"`{missing}` is missing.\n"
        f"{runtime_hint}\n"
        f"See the `[project.optional-dependencies]` table "
        f"in the package's `pyproject.toml` for the full list "
        f"of extras."
    )


def message_for(
    error: BaseException,
    package: str,
    extra: str,
    purpose: str | None,
    runtime_hint: str | None,
) -> str:
    """
    The message for a failed optional import.

    ``error`` is the exception the import raised. A
    ``ModuleNotFoundError`` names the module it could not
    find: when that is NOT the package itself, the package is
    installed and one of its own dependencies is missing —
    the runtime case. Everything else is the plain
    "not installed" case.
    """
    missing = getattr(error, "name", None)
    if (
        runtime_hint is not None
        and isinstance(missing, str)
        and missing
        and missing != package
    ):
        return format_missing_runtime(package, missing, purpose, runtime_hint)
    return format_missing_package(package, extra, purpose)


__all__ = [
    "format_missing_package",
    "format_missing_runtime",
    "message_for",
]
