# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event_log.dispatch -- Resilience layer for `EventLog.append`.

This module wraps the Redis XADD in:

  - a circuit breaker (when the caller supplies one);
  - a retry loop with timeout (`with_timeout_and_retry`
    using a `BackoffPolicy`);
  - a direct single-attempt call when neither breaker
    nor retry is configured.

Why a separate module?

  - The breaker / retry / direct branches share no
    logic with the `EventLog` class — they only differ
    by which knobs are wired in.
  - The `BackoffPolicy` dataclass is the contract: the
    `EventLog` constructs the default policy from the
    legacy kwargs and delegates here.
  - Tests can exercise the dispatch in isolation (a
    fake Redis + a fake breaker + a fake BackoffPolicy).

The `BackoffPolicy` replaces the previous
``append_retry_*`` kwargs. When a caller still passes
those kwargs, the `EventLog.__init__` translates them
into a `BackoffPolicy` before delegating here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Optional

from ...core.result import Err, Ok, PersistenceError, Result
from ...resilience import CircuitBreaker
from ...resilience.timeout import BackoffPolicy


async def dispatch_redis_call(
    redis_call: Callable[[], Awaitable[bytes]],
    *,
    circuit_breaker: Optional[CircuitBreaker] = None,
    append_backoff: Optional[BackoffPolicy] = None,
    append_timeout_seconds: float = 5.0,
) -> Result[bytes, PersistenceError]:
    """
    Async implementation of the dispatch orchestrator.

    The `EventLog.append` awaits this directly. Branches
    (in order):

      1. Circuit breaker: the breaker wins over retry
         (defence in depth: a single failure counted
         once, not retry-amplified).
      2. Retry with timeout + exponential backoff +
         jitter, bounded by an absolute budget
         (when `append_backoff.max_attempts >= 2`).
      3. Direct single-attempt call with a per-attempt
         timeout (no breaker, no retry).
    """
    if circuit_breaker is not None:
        breaker_result = await circuit_breaker.call(redis_call)
        if breaker_result.is_err():
            return Err(PersistenceError(f"circuit_open:{breaker_result.err_value()}"))
        return Ok(breaker_result.ok_value())

    from ...resilience import with_timeout_and_retry

    backoff = append_backoff
    if backoff is not None and backoff.max_attempts >= 2:
        try:
            stream_id = await with_timeout_and_retry(
                redis_call,
                timeout_seconds=append_timeout_seconds,
                backoff=backoff,
            )
            return Ok(stream_id)
        except (
            asyncio.TimeoutError,
            ConnectionError,
            TimeoutError,
        ) as e:
            return Err(PersistenceError(f"redis_timeout: {type(e).__name__}"))

    try:
        stream_id = await asyncio.wait_for(
            redis_call(),
            timeout=append_timeout_seconds,
        )
        return Ok(stream_id)
    except asyncio.TimeoutError:
        return Err(PersistenceError(f"redis_timeout after {append_timeout_seconds}s"))


__all__ = ["dispatch_redis_call"]
