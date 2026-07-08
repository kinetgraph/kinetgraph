# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
In-memory `EventLog` for unit tests.

Mirrors the public surface used by tests:
  - `append(event) -> Result` (always Ok, records in `events`).
  - `read(agent_id) -> list[Event]` (returns the recorded
    events for that agent).

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

    async def read(self, agent_id: str) -> list[Event]:
        return [e for e in self.events if e.agent_id == agent_id]

    async def read_latest(self, agent_id: str, count: int = 1) -> list[Event]:  # type: ignore[override]
        return [e for e in self.events if e.agent_id == agent_id][-count:]
