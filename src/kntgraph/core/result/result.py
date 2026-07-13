# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
result.result -- The `Result` class + `Ok` / `Err` helpers.

Wrapper around the third-party `result` library.
Combines the Railway Pattern with Design by Contract
for functional error flow.

Usage:
    result = (
        Result.try_(lambda: validate(data))
        .bind(lambda d: save(d))
        .map(lambda r: transform(r))
        .value_or(default)
    )

The exception classes (`RailwayError`,
`ValidationError`, `PersistenceError`, `BusinessError`,
`ToolError`, `UnwrapError`) live in `result.errors`.
They are typically used as the `E` parameter of
`Result[T, E]`.

Typing discipline
-----------------

The third-party ``result`` library's types are mapped
onto our own generic ``Result[T, E]``. Where the
underlying ``Ok`` / ``Err`` leave a slot unbounded,
we expose a fresh ``TypeVar`` so callers get precise
type inference at every chain.
"""

from __future__ import annotations

from typing import Callable, Generic, TypeVar

from result import Err as BaseErr
from result import Ok as BaseOk
from result import Result as BaseResult

from .errors import UnwrapError


T = TypeVar("T")
E = TypeVar("E", bound=Exception)
# ``map`` and ``match`` produce a value of a brand-new
# type (whatever ``func`` returns). They use a fresh
# ``TypeVar`` so the inference flows through the chain.
U = TypeVar("U")
# ``map_err`` swaps the error type for a brand-new one.
F = TypeVar("F", bound=Exception)
# ``match`` returns whatever the chosen branch returns.
R = TypeVar("R")


class Result(Generic[T, E]):
    """
    Result of an operation that may fail (wrapper around
    the `result` library).

    Railway Pattern: successes stay on the "right" track,
    errors stay on the "wrong" track.
    """

    def __init__(self, _result: BaseResult[T, E]):
        self._result = _result

    @classmethod
    def try_(
        cls,
        func: Callable[[], T],
        exception_type: type[Exception] | tuple[type[Exception], ...] = Exception,
    ) -> "Result[T, Exception]":
        """
        Executa função e captura exceções.

        Usage:

            result = Result.try_(lambda: risky_operation())
            if result.is_ok():
                value = result.ok_value()
            else:
                error = result.err_value()
        """
        try:
            return Ok(func())
        except exception_type as e:
            return Err(e)

    @classmethod
    def ok(cls, value: T) -> "Result[T, E]":
        """Build a success result."""
        return cls(BaseOk(value))

    @classmethod
    def err(cls, error: E) -> "Result[T, E]":
        """Build an error result."""
        return cls(BaseErr(error))

    def is_ok(self) -> bool:
        """Check whether this is a success."""
        return self._result.is_ok()

    def is_err(self) -> bool:
        """Check whether this is an error."""
        return self._result.is_err()

    def ok_value(self) -> T | None:
        """Return the value if Ok, None if Err."""
        return self._result.ok()  # type: ignore[no-untyped-call]

    def err_value(self) -> E | None:
        """Return the error if Err, None if Ok."""
        return self._result.err()  # type: ignore[no-untyped-call]

    def err_value_or_raise(self) -> E:
        """
        Returns the error value, asserting the Result is
        `Err`. Raises `UnwrapError` if the Result is `Ok`.

        Use this when you have already checked `is_err()`
        and want a non-Optional return type. The pyright
        and mypy signatures both narrow to `E` (no None).

            if r.is_err():
                return Err(r.err_value_or_raise())  # E, not E | None
        """
        if self.is_err():
            e = self._result.err()  # type: ignore[no-untyped-call]
            if e is not None:
                return e
        raise UnwrapError("err_value_or_raise called on an Ok Result")

    def map(self, func: Callable[[T], U]) -> "Result[U, E]":
        """
        Transform the success value.

        Usage:
            result.map(lambda x: x * 2)
        """
        if self.is_ok():
            v = self.ok_value()
            if v is not None:
                return Ok(func(v))
        return self._as_same_err()

    def map_err(self, func: Callable[[E], F]) -> "Result[T, F]":
        """
        Transform the error value.

        Usage:
            result.map_err(lambda e: CustomError(str(e)))
        """
        if self.is_err():
            e = self.err_value()
            if e is not None:
                return Err(func(e))
        return self._as_same_ok()

    def bind(self, func: Callable[[T], "Result[U, E]"]) -> "Result[U, E]":
        """
        Chain operations (flatMap).

        Usage:
            (
                Result.try_(lambda: validate(data))
                .bind(lambda d: save(d))
                .bind(lambda r: notify(r))
            )
        """
        if self.is_ok():
            v = self.ok_value()
            if v is not None:
                return func(v)
        return self._as_same_err()

    def value_or(self, default: T) -> T:
        """
        Return the value or a default on error.

        Usage:
            value = result.value_or(default_value)
        """
        if self.is_ok():
            v = self.ok_value()
            if v is not None:
                return v
        return default

    def unwrap(self) -> T:
        """
        Return the value or raise.

        Usage:
            value = result.unwrap()  # Raises if Err
        """
        if self.is_ok():
            v = self.ok_value()
            if v is not None:
                return v
        raise UnwrapError("Called unwrap on an error")

    def unwrap_or(self, default: T) -> T:
        """Return the value or a default (alias for value_or)."""
        return self.value_or(default)

    def unwrap_or_else(self, func: Callable[[E], T]) -> T:
        """
        Return the value or call a function to produce a default.

        Usage:
            value = result.unwrap_or_else(lambda e: handle_error(e))
        """
        if self.is_ok():
            v = self.ok_value()
            if v is not None:
                return v
        e = self.err_value()
        if e is not None:
            return func(e)
        raise UnwrapError("Result has neither value nor error")

    def expect(self, message: str) -> T:
        """
        Return the value or raise with a custom message.

        Usage:
            value = result.expect("Operation failed")
        """
        if self.is_ok():
            v = self.ok_value()
            if v is not None:
                return v
        raise UnwrapError(f"{message}: no value")

    def match(
        self,
        ok_func: Callable[[T], R],
        err_func: Callable[[E], R],
    ) -> R | None:
        """
        Pattern match.

        Usage:
            result.match(
                lambda v: print(f"Success: {v}"),
                lambda e: print(f"Error: {e}")
            )

        Returns ``None`` when neither branch fires (the
        Result is in an indeterminate state). Callers
        should always pair ``match`` with a prior
        ``is_ok()`` / ``is_err()`` check.
        """
        if self.is_ok():
            v = self.ok_value()
            if v is not None:
                return ok_func(v)
            return None
        e = self.err_value()
        if e is not None:
            return err_func(e)
        return None

    # ------------------------------------------------------------------
    # Internal re-shape helpers. Used by ``map`` / ``map_err`` /
    # ``bind`` so the new chain step has the right Ok-or-Err
    # branch narrowed to the original type without losing
    # information at the type level. The body is a plain
    # constructor call; the cost is one extra Ok/Err
    # construction per failure path, which is negligible.
    # ------------------------------------------------------------------

    def _as_same_err(self) -> "Result[U, E]":
        if self.is_err():
            return Err(self.err_value_or_raise())
        # Unreachable: the callers only invoke this on Err.
        raise UnwrapError("_as_same_err called on an Ok Result")

    def _as_same_ok(self) -> "Result[T, F]":
        if self.is_ok():
            return Ok(self.ok_value())  # type: ignore[arg-type]
        raise UnwrapError("_as_same_ok called on an Err Result")


def Ok(value: T) -> Result[T, E]:
    """Build a Result Ok (convenience function).

    The error slot is left generic (``E`` is unbound)
    so callers can chain ``.map_err(...)`` to specialise
    it without an explicit annotation.
    """
    return Result(BaseOk(value))


def Err(error: E) -> Result[T, E]:
    """Build a Result Err (convenience function).

    The value slot is left generic (``T`` is unbound)
    so callers can chain ``.map(...)`` to specialise
    it without an explicit annotation.
    """
    return Result(BaseErr(error))


# Aliases for convenience
Success = Ok
Failure = Err


__all__ = [
    "Err",
    "Failure",
    "Ok",
    "Result",
    "Success",
]
