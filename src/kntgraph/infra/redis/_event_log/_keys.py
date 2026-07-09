# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis key conventions for the EventLog.

Single source of truth for the wire format:

  - ``AGENT_STREAM_KEY``     : per-agent Redis Stream
  - ``EVENT_ID_INDEX``       : SET-based idempotency index
  - ``SCAN_PATTERN``         : glob pattern for listing agents
  - ``MAXLEN_DEFAULT``       : per-stream trim threshold

The previous location was ``stream/event_log/store.py``
(``AGENT_STREAM_KEY``, ``EVENT_ID_INDEX``). Moving the
constants here lets the storage adapter own the wire format
without leaking it back into ``stream/event_log``.
"""

from __future__ import annotations


AGENT_STREAM_KEY: str = "knt:agents:{agent_id}:events"
"""Per-agent event log Redis Stream key."""

EVENT_ID_INDEX: str = "knt:eventids:{event_id}"
"""SET-based idempotency index. Maps event_id → stream_id."""

SCAN_PATTERN: str = "knt:agents:*:events"
"""Glob pattern for ``scan_iter`` when listing all agents."""

MAXLEN_DEFAULT: int = 100_000
"""Default per-stream MAXLEN (auto-trim). Override per adapter."""


def stream_key_for_agent(agent_id: str) -> str:
    """Build the stream key for an agent."""
    return AGENT_STREAM_KEY.format(agent_id=agent_id)


def event_id_key(event_id: str) -> str:
    """Build the idempotency key for an event_id."""
    return EVENT_ID_INDEX.format(event_id=event_id)


def parse_agent_id_from_stream_key(key: str) -> str | None:
    """Extract the agent_id portion of a stream key.

    Returns ``None`` if the key does not match the expected
    pattern. Used by ``RedisEventLogAdapter.list_agents`` to
    decode the result of ``scan_iter``.
    """
    prefix = "knt:agents:"
    suffix = ":events"
    if key.startswith(prefix) and key.endswith(suffix):
        return key[len(prefix) : -len(suffix)]
    return None


__all__ = [
    "AGENT_STREAM_KEY",
    "EVENT_ID_INDEX",
    "MAXLEN_DEFAULT",
    "SCAN_PATTERN",
    "event_id_key",
    "parse_agent_id_from_stream_key",
    "stream_key_for_agent",
]
