# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Resilience patterns for system resilience.

Public surface
--------------

  Circuit breaker (per-instance or registry-backed):

    cb = await get_circuit_breaker("llm")
    result = await cb.call(llm.chat, prompt)
    state = cb.get_state()    # monitoring snapshot

  Retry:

    @retry_with_backoff(max_attempts=3)
    async def fetch(): ...

    await retry_async(fetch, max_attempts=3)

  Bulkhead:

    pool = await get_bulkhead("tenant-1")
    result = await pool.execute(some_work)

  Timeout:

    result = await with_timeout(slow_fn, timeout_seconds=5.0)
    result = await with_timeout_and_retry(
        slow_fn, timeout_seconds=5.0, max_attempts=3,
        base_delay=1.0, max_total_seconds=30.0,
    )

  Fallback:

    result = await with_fallback(primary, secondary, ...)
    result = await with_default_on_failure(primary, default_value)

  Exceptions:

    ``CircuitBreakerError`` and ``BulkheadFullError`` are
    exported for callers that want to ``raise`` them
    explicitly. The Railway-Pattern ``call`` / ``execute``
    paths return ``Err(BusinessError(...))`` instead; the
    exception classes are useful at the API edge.
"""

from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
    get_all_breakers,
    get_circuit_breaker,
    remove_circuit_breaker,
)
from .retry import (
    RetryConfig,
    retry_async,
    retry_fast,
    retry_normal,
    retry_slow,
    retry_with_backoff,
)
from .bulkhead import (
    BulkheadFullError,
    BulkheadPool,
    get_all_bulkheads,
    get_bulkhead,
    remove_bulkhead,
)
from .timeout import (
    BackoffPolicy,
    TimeoutConfig,
    timeout_fast,
    timeout_normal,
    timeout_slow,
    with_timeout,
    with_timeout_and_retry,
)
from .fallback import (
    with_default_on_failure,
    with_fallback,
    with_fallback_chain,
)

__all__ = [
    # Circuit breaker
    "CircuitBreaker",
    "CircuitBreakerError",
    "CircuitState",
    "get_circuit_breaker",
    "get_all_breakers",
    "remove_circuit_breaker",
    # Retry
    "retry_with_backoff",
    "retry_async",
    "RetryConfig",
    "retry_fast",
    "retry_normal",
    "retry_slow",
    # Bulkhead
    "BulkheadPool",
    "BulkheadFullError",
    "get_bulkhead",
    "get_all_bulkheads",
    "remove_bulkhead",
    # Timeout
    "with_timeout",
    "with_timeout_and_retry",
    "BackoffPolicy",
    "TimeoutConfig",
    "timeout_fast",
    "timeout_normal",
    "timeout_slow",
    # Fallback
    "with_fallback",
    "with_default_on_failure",
    "with_fallback_chain",
]
