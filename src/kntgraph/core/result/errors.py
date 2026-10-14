# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
result.errors -- Exception hierarchy for the railway flow.

Five exception classes that all inherit from
`RailwayError` (the abstract base). Use them as the
`E` type parameter of `Result[T, E]` to give callers
typed catches:

    try:
        await persist(event)
    except PersistenceError as e:
        ...

`UnwrapError` lives here too (it's a programming
error, not a domain error, but it's an exception
and fits the same module).

Why a separate module?

The `Result` class is generic over the error type.
The 5 domain errors are concrete types. Keeping
them in `result.errors` lets callers import the
exceptions without dragging the `Result`
implementation in:

    from kntgraph.core.result.errors import (
        PersistenceError,
    )

ToolError error_class
---------------------

``ToolError`` carries an ``error_class`` slot
(``Literal["transient", "permanent", "unknown"]``) so
the framework classifies failures without inspecting the
message string. ``permanent`` means the failure will
never succeed on retry (deterministic infra error
like ``NoSuchBucket``); ``transient`` means retry may
succeed (rate limit, timeout, network blip);
``unknown`` is the safe default.

The classification is read by the WorkerManager and
forwarded into the ``tool.<name>.failed`` event's
``data`` so downstream consumers (TTL sweeper,
circuit breaker, DLQ redaction) can act on it without
parsing the free-form ``error`` string.
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
    ones without parsing the free-form message string.
    """

    # Declared at class level so the attribute is
    # typed ``ToolErrorClass`` (not ``str``) for static
    # checkers — the body sets it again in ``__init__``,
    # but pyright/mypy infer the wider ``str`` from the
    # ``__init__`` body alone.
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
        """Factory for a transient (retryable) error."""
        return cls(message, error_class="transient")

    @classmethod
    def permanent(cls, message: str = "") -> ToolError:
        """Factory for a permanent (non-retryable) error.

        Examples: ``NoSuchBucket``, ``PermissionDenied``,
        ``InvalidArgument``, ``NotFound``. The framework
        MUST NOT retry; the failure routes straight to
        DLQ (or surfaces to the operator via the
        reactive layer).
        """
        return cls(message, error_class="permanent")

    @classmethod
    def unknown(cls, message: str = "") -> ToolError:
        """Factory for an unclassified error (the legacy
        default — used when the tool cannot determine
        whether the failure is retryable). The framework
        preserves the previous retry behaviour for this
        case (no escalation change).
        """
        return cls(message, error_class="unknown")


__all__ = [
    "BusinessError",
    "PersistenceError",
    "RailwayError",
    "ToolError",
    "ToolErrorClass",
    "UnwrapError",
    "ValidationError",
]
