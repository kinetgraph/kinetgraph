# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis EventLog adapter — sub-package re-exports.

Public API
----------

- :class:`EventLogStorage` — domain Protocol
- :class:`RedisEventLogAdapter` — Redis implementation
- :func:`stream_key_for_agent` / :func:`event_id_key` — key helpers
"""

from ._adapter import EventLogStorage, RedisEventLogAdapter
from ._keys import (
    AGENT_STREAM_KEY,
    EVENT_ID_INDEX,
    MAXLEN_DEFAULT,
    SCAN_PATTERN,
    event_id_key,
    parse_agent_id_from_stream_key,
    stream_key_for_agent,
)


__all__ = [
    # Adapter
    "EventLogStorage",
    "RedisEventLogAdapter",
    # Keys
    "AGENT_STREAM_KEY",
    "EVENT_ID_INDEX",
    "MAXLEN_DEFAULT",
    "SCAN_PATTERN",
    "event_id_key",
    "parse_agent_id_from_stream_key",
    "stream_key_for_agent",
]
