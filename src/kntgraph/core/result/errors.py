# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
result.errors -- Exception hierarchy for the railway flow.

Five exception classes that all inherit from
``RailwayError`` (the abstract base). Use them as the
``E`` type parameter of ``Result[T, E]``:

    try:
        await persist(event)
    except PersistenceError as e:
        ...

``UnwrapError`` lives here too (it's a programming
error, not a domain error, but it's an exception).

``ToolError`` carries an ``error_class`` slot
(``Literal["transient", "permanent", "unknown"]``) so
the framework classifies failures without parsing the
free-form message — ``permanent`` means the failure
will never succeed on retry (e.g. ``NoSuchBucket``);
``transient`` means retry may succeed; ``unknown`` is
the safe default. The classification is read by the
WorkerManager and forwarded into the
``tool.<name>.failed`` event's ``data``.
"""

from __future__ import annotations

from typing import Literal

# Module-level alias so the factories below reference a
# single source of truth (and external typecheckers
# don't have to import the private ``__init__``
# parameter name).
ToolErrorClass = Literal["transient", "permanent", "unknown"]


class UnwrapError(Exception):
    """Raised when unwrap() is called on an Err."""


class RailwayError(Exception):
    """Base for errors in the railway flow."""


class ValidationError(RailwayError):
    """Validation error."""


class PersistenceError(RailwayError):
    """Persistence error."""


class BusinessError(RailwayError):
    """Business rule error."""


class ToolError(RailwayError):
    """Error executing an external Tool.

    Carries an ``error_class`` (``transient`` /
    ``permanent`` / ``unknown``) so the framework can
    distinguish retryable failures from deterministic
    ones without parsing the free-form message.

    Construction::

        ToolError("boom")                        # legacy: unknown
        ToolError("boom", error_class="permanent")
        ToolError.transient("rate_limited")
        ToolError.permanent("no_such_bucket")
    """

    # Class attribute declared for static-typing —
    # the body sets it again in ``__init__``, but
    # pyright/mypy infer ``str`` from the body alone.
    error_class: ToolErrorClass

    def __init__(
        self,
        message: str = "",
        *,
        error_class: ToolErrorClass = "unknown",
    ) -> None:
        super().__init__(message)
        self.error_class = error_class

    @classmethod
    def transient(cls, message: str = "") -> ToolError:
        """Transient (retryable) error."""
        return cls(message, error_class="transient")

    @classmethod
    def permanent(cls, message: str = "") -> ToolError:
        """Permanent (non-retryable) error."""
        return cls(message, error_class="permanent")


__all__ = [
    "BusinessError",
    "PersistenceError",
    "RailwayError",
    "ToolError",
    "ToolErrorClass",
    "UnwrapError",
    "ValidationError",
]
