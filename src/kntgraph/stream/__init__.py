# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
FMH Stream — Redis-backed append-only event log.

The stream is the source of truth. The World is a derived value
obtained by folding events from the stream.
"""

from .event_log import (
    AGENT_STREAM_KEY,
    EVENT_ID_INDEX,
    EventLog,
)
from .projection import (
    fold_world,
    fold_world_for_agent,
    read_all_events,
    stream_agents,
)

__all__ = [
    "AGENT_STREAM_KEY",
    "EVENT_ID_INDEX",
    "EventLog",
    "fold_world",
    "fold_world_for_agent",
    "read_all_events",
    "stream_agents",
]
