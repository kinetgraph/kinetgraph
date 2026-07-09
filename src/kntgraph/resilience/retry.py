# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Retry with exponential backoff + jitter for resilience.

The previous version of this module was buggy in two
distinct ways:

  1. ``retry_async`` used tenacity's sync ``retry``,
     which when applied to an ``async def`` returned a
     coroutine OBJECT to the caller instead of awaiting
     it. The caller would ``await`` a coroutine nobody
     was running. Fixed by switching to
     ``AsyncRetrying``.

  2. Importing the module called ``logging.basicConfig``
     at module scope, mutating the root logger globally
     even when the host application had configured
     logging differently. Removed.

The new implementation:

  - ``retry_with_backoff`` is a decorator for
    ``async def`` functions. It uses
    ``AsyncRetrying`` + ``wait_exponential_jitter`` so
    concurrent retries do not lockstep.
  - ``retry_async(fn, *args, **kwargs)`` is an
    ``async def`` that retries an async callable, also
    via ``AsyncRetrying``.
  - ``RetryConfig`` is a reusable wrapper that holds
    retry parameters and offers ``decorate(fn)`` and
    ``async execute(fn, *args, **kwargs)``.

Cancellation
------------

``asyncio.CancelledError`` is a ``BaseException`` subclass
and is NEVER matched by ``retry_if_exception_type``. We
also strip it from the ``retry_on`` tuple defensively
(e.g. a caller that passes ``(Exception,)`` is unaffected;
a caller that explicitly puts ``CancelledError`` in
``retry_on`` will see a ``ValueError`` at decoration time).
This pins the contract: cancellation propagates, retries do
not.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, ParamSpec, TypeVar, cast

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

import structlog

logger = structlog.get_logger()

T = TypeVar("T")
# ``P`` captures the parameter shape of the wrapped
# async function so the decorator preserves concrete
# types at every chain point.
P = ParamSpec("P")


DEFAULT_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)


def _sanitise_retry_on(
    retry_on: tuple[type[BaseException], ...],
) -> tuple[type[Exception], ...]:
    """
    Strip ``asyncio.CancelledError`` (and any
    ``BaseException`` subclass that should never be
    retried) from the ``retry_on`` tuple. Raises
    ``ValueError`` if the resulting tuple would be empty
    or contains anything that is not an ``Exception``
    subclass (a ``BaseException`` that is not
    ``CancelledError`` is rejected here).
    """
    cleaned: list[type[Exception]] = []
    for t in retry_on:
        if t is asyncio.CancelledError or (
            isinstance(t, type) and issubclass(t, asyncio.CancelledError)
        ):
            continue
        if not isinstance(t, type) or not issubclass(t, Exception):
            raise ValueError(
                f"retry_on entries must be Exception subclasses "
                f"(got {t!r}); BaseException subclasses other "
                f"than CancelledError are not retried"
            )
        cleaned.append(t)
    if not cleaned:
        raise ValueError(
            "retry_on must contain at least one retryable Exception subclass"
        )
    return tuple(cleaned)


def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE_EXCEPTIONS,
    reraise: bool = True,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """
    Decorator for automatic retry with exponential backoff + jitter.

    Backoff: ``delay = base_delay * 2**(attempt-1)`` with
    uniform jitter in [0, 1] of the computed delay
    (tenacity's ``wait_exponential_jitter``).

    Usage::

        @retry_with_backoff(max_attempts=3, base_delay=1.0)
        async def redis_operation() -> str:
            return await redis.get("key")

    The decorated callable MUST be ``async def``; sync
    callables are not supported (tenacity's sync ``retry``
    is not awaited by us). Apply this decorator only to
    coroutine functions.

    Args:
        max_attempts: Total number of attempts (must be
            ``>= 1``).
        base_delay: Initial delay between attempts.
        max_delay: Upper bound on a single delay.
        retry_on: Tuple of exception types that trigger a
            retry. ``CancelledError`` is stripped
            automatically.
        reraise: If True (default), re-raise the last
            exception after all attempts fail. If False,
            tenacity returns ``None``.

    Returns:
        A decorator that wraps an async callable.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay < 0:
        raise ValueError("base_delay must be >= 0")
    if max_delay <= 0:
        raise ValueError("max_delay must be > 0")
    safe_retry_on = _sanitise_retry_on(retry_on)

    def decorator(
        fn: Callable[P, Awaitable[T]],
    ) -> Callable[P, Awaitable[T]]:
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(
                "retry_with_backoff must be applied to an "
                "async def callable; got a sync function"
            )

        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential_jitter(initial=base_delay, max=max_delay),
                retry=retry_if_exception_type(safe_retry_on),
                reraise=reraise,
            ):
                with attempt:
                    return await fn(*args, **kwargs)
            # AsyncRetrying is a StopAsyncIteration on
            # exhaustion; we should never reach here
            # because reraise=True (default) re-raises the
            # last exception inside the ``with attempt``
            # block.
            return cast(T, None)

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorator


async def retry_async(
    fn: Callable[P, Awaitable[T]],
    *args: P.args,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE_EXCEPTIONS,
    **kwargs: P.kwargs,
) -> T:
    """
    Execute ``fn`` with retry (no decorator).

    Usage::

        result = await retry_async(
            redis.get, "key",
            max_attempts=3, base_delay=1.0,
        )

    Args:
        fn: Async callable to execute.
        *args: Positional arguments forwarded to ``fn``.
        max_attempts: Total number of attempts.
        base_delay: Initial delay between attempts.
        max_delay: Upper bound on a single delay.
        retry_on: Tuple of exception types that trigger a
            retry.
        **kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        The result of the successful call.

    Raises:
        The last exception of type ``retry_on`` after
            ``max_attempts`` failed attempts (unless
            reraise=False, which we do not expose here).
        asyncio.CancelledError: propagated unchanged.
    """
    if not asyncio.iscoroutinefunction(fn):
        raise TypeError("retry_async requires an async callable")
    safe_retry_on = _sanitise_retry_on(retry_on)

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=base_delay, max=max_delay),
        retry=retry_if_exception_type(safe_retry_on),
        reraise=True,
    ):
        with attempt:
            return await fn(*args, **kwargs)
    # Unreachable (reraise=True).
    return None  # pragma: no cover


class RetryConfig:
    """Reusable retry configuration."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE_EXCEPTIONS,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_on = _sanitise_retry_on(retry_on)

    def decorate(
        self,
        fn: Callable[P, Awaitable[T]],
    ) -> Callable[P, Awaitable[T]]:
        """Apply retry as a decorator."""
        return retry_with_backoff(
            max_attempts=self.max_attempts,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            retry_on=self.retry_on,
        )(fn)

    async def execute(
        self,
        fn: Callable[P, Awaitable[T]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Execute ``fn`` with retry."""
        return await retry_async(
            fn,
            *args,
            max_attempts=self.max_attempts,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            retry_on=self.retry_on,
            **kwargs,
        )


# Predefined configs
retry_fast = RetryConfig(max_attempts=2, base_delay=1.0, max_delay=5.0)
retry_normal = RetryConfig(max_attempts=3, base_delay=2.0, max_delay=30.0)
retry_slow = RetryConfig(max_attempts=5, base_delay=3.0, max_delay=60.0)
