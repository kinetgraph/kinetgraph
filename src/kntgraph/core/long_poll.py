# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Long-poll helper — the canonical way to wait for a
specific Event on the EventLog.

Two call sites previously implemented the same
deadline-driven polling loop:

  - `fmh_app.llm.dispatcher.LLMDispatcher._long_poll` —
    used by the LLM intent flow to wait for a
    terminal event whose `correlation_id` matches the
    intent's `event_id`.
  - `kntgraph.api.intent_router.create_app` —
    used by the HTTP gateway to wait for a
    `tool.{name}.completed` / `.failed` event whose
    `causation_id` matches the request's `event_id`.

Both loops did the same thing:
  1. compute a deadline = now + timeout_s
  2. while within the deadline:
     a. read events from the log
     b. find one that matches the correlation /
        causation id and has a terminal event type
     c. return the event, or
     d. sleep for the poll interval and try again
  3. on deadline, return a "pending" / "timeout" result

Differences between the call sites:
  - match key (`correlation_id` vs `causation_id`)
  - terminal event type predicate
  - return type (`Result[Event, str]` vs
    `StatusResponse`)
  - poll interval (configurable vs hardcoded 0.1s)
  - deadline clock (`time.monotonic` vs
    `asyncio.get_event_loop().time`)

The helper exposes the loop as an async generator
that yields the matching event when found. The
caller decides what to wrap the result in
(`Result`, `StatusResponse`, etc.) and which match
predicate to use.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Optional

from .event import Event


# Default poll interval when the caller doesn't
# specify one. 100ms is fast enough for interactive
# HTTP requests without spinning the event loop.
DEFAULT_POLL_INTERVAL_S: float = 0.1


async def await_terminal_event(
    *,
    read: Callable[[], Awaitable[list[Event]]],
    predicate: Callable[[Event], bool],
    timeout_s: float,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> Optional[Event]:
    """
    Poll `read()` until a terminal event matches
    `predicate`, or `timeout_s` elapses.

    Returns the matching `Event`, or `None` on
    timeout. The caller maps the result to its own
    return type (`Result[Event, str]`,
    `StatusResponse`, etc.).

    `read()` should return the events visible to
    this caller at this moment — typically a per-
    agent read from the EventLog
    (`log.read(agent_id)`). The helper does not
    own the read; it just loops on whatever the
    caller gives it.

    `predicate` is the match + terminal check. The
    helper applies it to every event returned by
    `read()`; the first match wins. The caller can
    match on `correlation_id` (LLM dispatcher),
    `causation_id` (HTTP gateway), or any other
    field. The helper is intentionally agnostic to
    the match key.

    `poll_interval_s` defaults to 100ms. The
    dispatcher sets it explicitly (also 100ms in
    production); the HTTP gateway used a hardcoded
    100ms before this helper existed.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = await read()
        for e in events:
            if predicate(e):
                return e
        await asyncio.sleep(poll_interval_s)
    return None


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "await_terminal_event",
]
