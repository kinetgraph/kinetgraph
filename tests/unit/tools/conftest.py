# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration and shared fixtures for tool unit tests.
"""

from __future__ import annotations

import pytest

from kntgraph.core.event import Event
from kntgraph.core.result import Ok


class _FakeLog:
    """
    Minimal stand-in for EventLog. Records appended events
    and returns a fake stream id. Used in unit tests that
    exercise the invoker without a real Redis.
    """

    def __init__(self) -> None:
        self.appended: list[Event] = []

    async def append(self, event: Event):
        self.appended.append(event)
        return Ok(f"fake-{len(self.appended)}-0")

    async def read(self, agent_id: str) -> list[Event]:
        return [e for e in self.appended if e.agent_id == agent_id]


@pytest.fixture
def fake_log() -> _FakeLog:
    return _FakeLog()
