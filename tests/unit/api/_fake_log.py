# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
In-memory `EventLog` for unit tests.

Mirrors the public surface used by tests:
  - `append(event) -> Result` (always Ok, records in `events`).
  - `read(agent_id) -> list[Event]` (returns the recorded
    events for that agent).
  - `read(agent_id, start="-", end="+")` returns the
    recorded events for that agent in the [start,
    end] range, using the same exclusive-id
    convention as Redis Streams: ``start="("`` means
    "strictly after the cursor id" (the SSE
    subscribe endpoint relies on this).

Not a real implementation. Use only in tests.
"""

from __future__ import annotations


from kntgraph.core.event import Event
from kntgraph.core.result import Ok


class FakeEventLog:
    """Minimal in-memory EventLog for tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def append(self, event: Event) -> "Ok[None]":  # type: ignore[override]
        self.events.append(event)
        return Ok(None)

    async def append_batch(self, events: list[Event]) -> "Ok[None]":  # type: ignore[override]
        self.events.extend(events)
        return Ok(None)

    async def read(
        self,
        agent_id: str,
        start: str = "-",
        end: str = "+",
        count: int | None = None,
    ) -> list[Event]:
        """Read events for ``agent_id`` in [start, end].

        ``start`` follows Redis Stream convention:

          - ``"-"`` / ``"0-0"`` / ``"0"``: from the
            beginning.
          - ``"(<event_id>"``: strictly after the
            cursor ``<event_id>``. The SSE
            subscribe endpoint uses this to read
            only events emitted since the last
            yielded ``id``.

        ``end`` is ignored in the in-memory fake
        (returns everything to the tip of the
        log). ``count`` caps the returned list
        size.
        """
        all_events = [e for e in self.events if e.agent_id == agent_id]
        if start.startswith("("):
            cursor_id = start[1:]
            all_events = [e for e in all_events if str(e.event_id) > cursor_id]
        if count is not None:
            all_events = all_events[:count]
        return all_events

    async def read_latest(self, agent_id: str, count: int = 1) -> list[Event]:
        return [e for e in self.events if e.agent_id == agent_id][-count:]
