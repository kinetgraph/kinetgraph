# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the resilience wrappers around
`kntgraph.stream.event_log.EventLog.append`.

Covers: retry (timeout + transient error), circuit breaker,
idempotency dedup interaction, and the Ok/Err Result return
contract.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest


from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Err, Ok, PersistenceError, Result
from kntgraph.resilience import CircuitBreaker
from kntgraph.resilience.timeout import BackoffPolicy
from kntgraph.stream.event_log import EventLog

pytestmark = pytest.mark.asyncio


def _make_event() -> Event:
    """Minimal valid event for append."""
    return Event.create(
        event_class="domain",
        event_type="test.event.created",
        agent_id="agent-1",
        data={"k": "v"},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


class _FakeStorage:
    """Fake EventLogStorage for resilience tests.

    Configurable: ``append_fn`` is the async function
    called by `append`. Defaults to returning Ok("1-0").
    """

    def __init__(self, append_fn=None):
        self.append_fn = append_fn or self._default_append
        self.append_calls: list[dict] = []
        self.read_calls: list[str] = []
        self.read_latest_calls: list[tuple[str, int]] = []

    async def _default_append(self, **kwargs):
        return Ok("1-0")

    async def append(
        self, *, agent_id: str, event: Event
    ) -> Result[str, PersistenceError]:
        self.append_calls.append(
            {"agent_id": agent_id, "event_id": str(event.event_id)}
        )
        return await self.append_fn(agent_id=agent_id, event=event)

    async def read(self, agent_id, *, start="-", end="+", count=None):
        self.read_calls.append(agent_id)
        return []

    async def read_latest(self, agent_id, n=1):
        self.read_latest_calls.append((agent_id, n))
        return []

    async def stream_len(self, agent_id):
        return 0

    async def list_agents(self):
        return []

    async def delete(self, agent_id):
        pass


def _make_log(storage: _FakeStorage, **kwargs) -> EventLog:
    """Construct an EventLog with the fake storage."""
    timeout = kwargs.pop("append_timeout_seconds", 0.05)
    max_attempts = kwargs.pop("append_retry_attempts", 2)
    if max_attempts < 1:
        max_attempts = 1
    policy = BackoffPolicy(
        max_attempts=max_attempts,
        base_delay=kwargs.pop("append_retry_base_delay", 0.001),
        max_delay=kwargs.pop("append_retry_max_delay", 0.01),
        max_total_seconds=kwargs.pop("append_retry_max_total_seconds", 1.0),
    )
    return EventLog(
        storage=storage,
        append_timeout_seconds=timeout,
        append_backoff=policy,
        circuit_breaker=kwargs.get("circuit_breaker"),
    )


class TestAppendResilienceWiring:
    async def test_no_breaker_no_retry_succeeds_on_first_try(self):
        storage = _FakeStorage()
        log = _make_log(storage, append_retry_attempts=0)
        event = _make_event()

        r = await log.append(event)

        assert r.is_ok()
        assert r.ok_value() == "1-0"
        # Exactly one call (no retries).
        assert len(storage.append_calls) == 1

    async def test_retry_recovers_from_timeout(self):
        """A simulated timeout on the first attempt is
        retried and the second attempt succeeds.
        """
        call_count = {"n": 0}

        async def flaky_append(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                await asyncio.sleep(0.05)  # > timeout
            return Result.ok(f"{call_count['n']}-0")

        storage = _FakeStorage(append_fn=flaky_append)
        log = _make_log(
            storage,
            append_timeout_seconds=0.01,
            append_retry_attempts=2,
            append_retry_base_delay=0.001,
            append_retry_max_delay=0.01,
        )
        event = _make_event()

        r = await log.append(event)

        assert r.is_ok()
        assert call_count["n"] == 2  # 1 timeout + 1 success

    async def test_retry_exhausted_returns_err(self):
        async def always_hangs(**kwargs):
            await asyncio.sleep(0.05)
            return Result.err(PersistenceError("never reached"))

        storage = _FakeStorage(append_fn=always_hangs)
        log = _make_log(
            storage,
            append_timeout_seconds=0.01,
            append_retry_attempts=1,
            append_retry_base_delay=0.001,
            append_retry_max_delay=0.01,
        )
        event = _make_event()

        r = await log.append(event)

        assert r.is_err()
        assert "redis_timeout" in str(r.err_value())

    async def test_circuit_breaker_short_circuits(self):
        """A tripped breaker rejects the append without
        touching the storage.
        """
        from kntgraph.resilience import CircuitState

        breaker = CircuitBreaker(
            "test-event-log",
            failure_threshold=1,
            recovery_timeout_seconds=10.0,
        )

        # Trip the breaker by recording one failure.
        async def boom() -> None:
            raise ConnectionError("redis down")

        r = await breaker.call(boom)
        assert r.is_err()
        assert breaker.state == CircuitState.OPEN

        # Now an `append` should be rejected without
        # touching storage.
        storage = _FakeStorage()
        log = _make_log(
            storage,
            circuit_breaker=breaker,
            append_retry_attempts=0,
        )
        event = _make_event()

        r = await log.append(event)

        assert r.is_err()
        # Redis was NEVER called.
        assert len(storage.append_calls) == 0

    async def test_retry_respects_max_total_seconds(self):
        """Even with many attempts allowed, the budget
        caps the worker time.
        """

        async def always_hangs(**kwargs):
            await asyncio.sleep(0.5)
            return Result.err(PersistenceError("never reached"))

        storage = _FakeStorage(append_fn=always_hangs)
        log = _make_log(
            storage,
            append_timeout_seconds=0.05,
            append_retry_attempts=10,
            append_retry_base_delay=10.0,
            append_retry_max_delay=60.0,
            append_retry_max_total_seconds=0.1,
        )
        event = _make_event()

        started = time.perf_counter()
        r = await log.append(event)
        elapsed = time.perf_counter() - started

        assert r.is_err()
        # Must NOT have spent 10+ seconds in retries.
        assert elapsed < 3.0, f"retry loop exceeded budget: {elapsed}s"

    async def test_idempotency_conflict_not_retried(self):
        """Idempotency conflicts are NOT retried — they
        are a normal control-flow signal from the storage.
        """

        async def conflict(**kwargs):
            return Err(PersistenceError("Concurrent insert in flight"))

        storage = _FakeStorage(append_fn=conflict)
        log = _make_log(storage, append_retry_attempts=5)
        event = _make_event()

        r = await log.append(event)

        assert r.is_err()
        # Exactly one call (no retry on idempotency conflict).
        assert len(storage.append_calls) == 1
