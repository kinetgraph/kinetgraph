# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Railway Oriented Programming utilities.

Combines the Railway Pattern with Design by Contract for
functional error flow.

Usage:
    result = (
        Result.try_(lambda: validate(data))
        .bind(lambda d: save(d))
        .map(lambda r: transform(r))
        .value_or(default)
    )

This module is a thin facade. The implementation is
split across:

  - `result.result`  — the `Result` class + `Ok` /
    `Err` helpers + `Success` / `Failure` aliases.
  - `result.errors`  — `UnwrapError`, `RailwayError`
    and the 4 domain errors (`ValidationError`,
    `PersistenceError`, `BusinessError`,
    `ToolError`).

The exception classes are typically used as the `E`
type parameter of `Result[T, E]`. Importing them
from this facade is the canonical path:

    from kntgraph.core.result import (
        Result, Ok, Err, PersistenceError,
    )
"""

from .errors import (
    BusinessError,
    PersistenceError,
    RailwayError,
    ToolError,
    UnwrapError,
    ValidationError,
)
from .result import Err, Failure, Ok, Result, Success

__all__ = [
    "BusinessError",
    "Err",
    "Failure",
    "Ok",
    "PersistenceError",
    "RailwayError",
    "Result",
    "Success",
    "ToolError",
    "UnwrapError",
    "ValidationError",
]
