# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Fallback strategies for resilience.

A "fallback" is a recovery path executed when a primary
operation fails. This module exposes a small, composable
surface — `with_fallback`, `with_default_on_failure`, and
`with_fallback_chain` — that covers the four common shapes
the project used to spell explicitly as
`FallbackStrategy.fallback_to_cache`,
`fallback_to_heuristic`, `fallback_to_default` and
`fallback_to_simplified`. All four reduced to the same body
(try primary → log success → except → log warn + return
secondary) plus a single string for logging.

The functions return `T` (the result of the first function
that succeeded), not `Result[T, E]`. Fallbacks are a
recovery contract, not a control-flow channel: the caller
has already chosen to swallow the original error by
invoking a fallback. If you need the original error in the
call site, do not use a fallback — wrap the primary in
`Result.try_` instead and let the caller decide.

ParamSpec and the `operation_name` keyword
------------------------------------------
`with_fallback` and `with_default_on_failure` are generic
over the callable's parameter set (`P`). The
`operation_name` keyword (operation name, used in logs) is
reserved by these helpers and is consumed from `**kwargs`
before forwarding the rest to the callables. The Python
grammar forbids a keyword-only parameter between
`*P.args` and `**P.kwargs`, so `operation_name` has to be
part of `**kwargs` for the ParamSpec contract to hold.

Call sites that want to set the operation name do so as a
normal keyword:

    await with_fallback(get_from_db, get_from_cache,
                        user_id, operation_name="user.fetch")

The `operation_name` key is popped from `kwargs` inside
the helper before forwarding the remaining kwargs to
`primary` and `secondary`. If `primary` itself accepts an
`operation_name` parameter, it would receive it — this is
a documented naming collision, not a bug.

PII in logs
-----------
We deliberately log the exception TYPE only
(``error_type=type(e).__name__``), not the full
``str(exception)``. Exceptions raised by user code (LLM
tool errors, validation errors with user input, DB
errors with connection strings) may carry PII or
secrets; the structlog pipeline is not redacted by
default. If you need the message for debugging, log it
at DEBUG with a redaction policy configured in the
host application's structlog processor.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import structlog

logger = structlog.get_logger()

T = TypeVar("T")
P = ParamSpec("P")
Stage = tuple[Callable[[], Awaitable[T]], str]


async def with_fallback(
    primary: Callable[P, Awaitable[T]],
    secondary: Callable[P, Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """
    Run `primary`; on any exception, run `secondary` with
    the same positional and keyword arguments.

    The reserved keyword `operation_name` (if present in
    `kwargs`) is popped and used as the operation name in
    logs; it is not forwarded to the callables.

    Returns the result of `primary` if it succeeded, else
    the result of `secondary`. If both raise, the
    `secondary`'s exception propagates.
    """
    op = kwargs.pop("operation_name", "operation")
    try:
        result = await primary(*args, **kwargs)
        logger.info("fallback.primary_ok", op=op)
        return result
    except Exception as primary_err:
        logger.warning(
            "fallback.primary_failed",
            op=op,
            error_type=type(primary_err).__name__,
        )
        return await secondary(*args, **kwargs)


async def with_default_on_failure(
    primary: Callable[P, Awaitable[T]],
    default: T,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """
    Run `primary`; on any exception, return `default`.

    Equivalent to `with_fallback` with a constant
    secondary value, but the call site is shorter and the
    intent (synthetic default, not a recovery
    computation) is explicit. Use this for "return `{}` /
    `0` / `False` / a placeholder object" shapes.

    `operation_name` is consumed from `**kwargs` (see
    `with_fallback` for the contract).
    """
    op = kwargs.pop("operation_name", "operation")
    try:
        result = await primary(*args, **kwargs)
        logger.info("fallback.primary_ok", op=op)
        return result
    except Exception as primary_err:
        logger.warning(
            "fallback.primary_failed_using_default",
            op=op,
            error_type=type(primary_err).__name__,
        )
        return default


async def with_fallback_chain(
    *stages: Stage,
    default: T | None = None,
) -> T | None:
    """
    Try each `(fn, name)` in order; the first one that
    succeeds wins. If all raise, return `default`.

    `fn` is called with NO arguments — the chain is
    intended for pre-bound callables (e.g. lambdas or
    partials) and for the case where the recovery
    computation differs structurally from the primary
    one. For the "all stages take the same args" case,
    prefer `with_fallback` directly.
    """
    for fn, name in stages:
        try:
            result: T = await fn()
            logger.info("fallback.chain.stage_ok", stage=name)
            return result
        except Exception as stage_err:
            logger.warning(
                "fallback.chain.stage_failed",
                stage=name,
                error_type=type(stage_err).__name__,
            )

    logger.warning("fallback.chain.all_failed")
    return default
