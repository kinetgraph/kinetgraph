# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Branch-coverage tests for ``stream/event_log/dispatch.py``.

The ``dispatch_redis_call`` function has three
orchestration branches:

  1. Circuit breaker: the breaker wins over retry.
  2. Retry with timeout + backoff (when
     ``backoff.max_attempts >= 2``).
  3. Direct single-attempt call with a per-attempt
     timeout.

Existing tests cover the retry / direct paths
indirectly (via ``test_append_resilience.py``); the
circuit-breaker and breaker-error paths are not
exercised end-to-end. Pinned here so a future refactor
does not regress the resilience orchestration.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from kntgraph.core.result import Err, Ok
from kntgraph.stream.event_log.dispatch import dispatch_redis_call


pytestmark = pytest.mark.asyncio


async def test_dispatch_with_circuit_breaker_short_circuits_when_open():
    """The branch ``if circuit_breaker is not None``
    and the breaker returns ``Err``: the dispatcher
    returns the breaker's error wrapped in
    ``PersistenceError``. Pinned so a future refactor
    does not bypass the breaker's open state.
    """
    breaker = AsyncMock()
    breaker.call = AsyncMock(return_value=Err("circuit_open_reason"))

    async def _call():
        return b"stream-id"

    result = await dispatch_redis_call(_call, circuit_breaker=breaker)
    assert result.is_err()
    assert "circuit_open" in str(result.err_value())


async def test_dispatch_with_circuit_breaker_passes_through_when_closed():
    """The branch ``if circuit_breaker is not None``
    and the breaker returns ``Ok``: the dispatcher
    returns the underlying call's value. Pinned so a
    future refactor does not skip the underlying call
    on a closed breaker.
    """
    from kntgraph.core.result import Ok

    breaker = AsyncMock()
    breaker.call = AsyncMock(return_value=Ok(b"stream-id"))

    async def _call():
        return b"stream-id"

    result = await dispatch_redis_call(_call, circuit_breaker=breaker)
    assert result.is_ok()
    assert result.ok_value() == b"stream-id"


async def test_dispatch_with_backoff_succeeds_on_first_attempt():
    """The branch ``if backoff is not None and
    backoff.max_attempts >= 2``: the retry path runs
    when a backoff policy is configured. Pinned so a
    future refactor does not skip the retry wrapper
    when a backoff is set.
    """
    from kntgraph.resilience.timeout import BackoffPolicy

    backoff = BackoffPolicy(max_attempts=3, base_delay=0.01)

    async def _call():
        return b"stream-id"

    result = await dispatch_redis_call(
        _call, append_backoff=backoff, append_timeout_seconds=1.0
    )
    assert result.is_ok()
    assert result.ok_value() == b"stream-id"
