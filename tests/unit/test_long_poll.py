# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the long-poll helper
(`kntgraph.core.long_poll.await_terminal_event`).

The helper centralises the deadline-driven polling
loop that was previously open-coded in two call
sites (`fmh_app.llm.dispatcher.LLMDispatcher._wait_terminal`
and `kntgraph.api.intent_router`). These tests
verify the loop's contract independently of any
specific consumer.
"""

from __future__ import annotations

import time
import uuid

import pytest


from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.long_poll import (
    DEFAULT_POLL_INTERVAL_S,
    await_terminal_event,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    event_type: str,
    correlation_id: str = "corr-1",
    causation_id: str = "cause-1",
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id="a-1",
        event_class="domain",
        data={},
        causation_id=causation_id,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


class _Counter:
    """Tracks how many times a callable was invoked."""

    def __init__(self) -> None:
        self.n = 0


# ---------------------------------------------------------------------------
# Match + return
# ---------------------------------------------------------------------------


class TestAwaitTerminalEvent:
    async def test_returns_matching_event(self):
        target = _event("cnpj.batch.fetched")

        async def read():
            return [target]

        r = await await_terminal_event(
            read=read,
            predicate=lambda e: e is target,
            timeout_s=1.0,
        )
        assert r is target

    async def test_skips_non_matching_events(self):
        other = _event("other.event", causation_id="other-cause")
        target = _event("cnpj.batch.fetched")

        async def read():
            return [other, target]

        r = await await_terminal_event(
            read=read,
            predicate=lambda e: str(e.causation_id) == "cause-1",
            timeout_s=1.0,
        )
        assert r is target

    async def test_returns_none_on_timeout(self):
        async def read():
            return []  # never finds a match

        started = time.monotonic()
        r = await await_terminal_event(
            read=read,
            predicate=lambda e: True,
            timeout_s=0.2,
            poll_interval_s=0.05,
        )
        elapsed = time.monotonic() - started
        assert r is None
        # The loop polls until the deadline; the
        # total elapsed time is bounded by the
        # timeout (not the poll interval).
        assert elapsed >= 0.2
        assert elapsed < 0.5, f"loop overshot deadline: {elapsed}s"

    async def test_first_match_wins(self):
        first = _event("first", causation_id="c")
        second = _event("second", causation_id="c")

        async def read():
            return [first, second]

        r = await await_terminal_event(
            read=read,
            predicate=lambda e: True,
            timeout_s=1.0,
        )
        assert r is first


# ---------------------------------------------------------------------------
# Polling semantics
# ---------------------------------------------------------------------------


class TestPolling:
    async def test_polls_until_match(self):
        """The reader is called repeatedly until a
        match is found. The poll interval caps how
        often."""
        seq = [
            [],
            [],
            [_event("cnpj.batch.fetched")],
        ]
        idx = {"i": 0}
        calls = _Counter()

        async def read():
            calls.n += 1
            i = idx["i"]
            if i < len(seq):
                idx["i"] = i + 1
                return seq[i]
            return []

        r = await await_terminal_event(
            read=read,
            predicate=lambda e: e.event_type == "cnpj.batch.fetched",
            timeout_s=1.0,
            poll_interval_s=0.01,
        )
        assert r is not None
        assert r.event_type == "cnpj.batch.fetched"
        # 3 reads (the first two return [], the
        # third returns the match).
        assert calls.n == 3

    async def test_poll_interval_caps_read_rate(self):
        """With a poll_interval of 50ms and a
        timeout of 0.2s, we expect at most 5 reads
        (one per interval). The actual number may be
        4-5 depending on scheduling."""
        calls = _Counter()

        async def read():
            calls.n += 1
            return []

        await await_terminal_event(
            read=read,
            predicate=lambda e: True,
            timeout_s=0.2,
            poll_interval_s=0.05,
        )
        # 200ms / 50ms = 4 intervals; allow 4-6 for
        # scheduling jitter.
        assert 4 <= calls.n <= 6, f"reads={calls.n}"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_poll_interval_is_100ms(self):
        """`DEFAULT_POLL_INTERVAL_S` is the
        documented default. Callers (the HTTP
        gateway) used 0.1s before this helper
        existed; we keep that value to avoid
        behaviour change."""
        assert DEFAULT_POLL_INTERVAL_S == 0.1
