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
"""

from __future__ import annotations


class UnwrapError(Exception):
    """Raised when unwrap() is called on an Err."""

    pass


class RailwayError(Exception):
    """Base for errors in the railway flow."""

    pass


class ValidationError(RailwayError):
    """Validation error."""

    pass


class PersistenceError(RailwayError):
    """Persistence error."""

    pass


class BusinessError(RailwayError):
    """Business rule error."""

    pass


class ToolError(RailwayError):
    """Error executing an external Tool."""

    pass


__all__ = [
    "BusinessError",
    "PersistenceError",
    "RailwayError",
    "ToolError",
    "UnwrapError",
    "ValidationError",
]
