# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis adapter — public API.

Sub-modules
-----------

- :mod:`._client`        — ``RedisLike`` Protocol (typed boundary)
- :mod:`._pool`          — ``RedisPool`` connection pool + factory
- :mod:`._codec`         — bytes↔str helpers
- :mod:`._errors`        — typed errors
- :mod:`._factory`       — high-level factories (settings-driven)
- :mod:`._event_log`     — EventLog storage adapter

The framework never imports ``redis.asyncio`` outside this
package; every Redis consumer accepts a ``RedisLike``
Protocol or an ``EventLogStorage`` Protocol.
"""

from __future__ import annotations

from ._client import PipelineLike, RedisLike
from ._codec import decode_dict, decode_int_dict, decode_value
from ._dlq import (
    DLQStorage,
    RedisDLQStorage,
)
from ._errors import IdempotencyConflict, RedisAdapterError, RedisUnavailableError
from ._event_log import (
    AGENT_STREAM_KEY,
    EVENT_ID_INDEX,
    EventLogStorage,
    MAXLEN_DEFAULT,
    RedisEventLogAdapter,
    SCAN_PATTERN,
    event_id_key,
    parse_agent_id_from_stream_key,
    stream_key_for_agent,
)
from ._event_log._idempotency import claim_event_id_slot
from ._factory import (
    create_continuity_storage,
    create_dlq_storage,
    create_event_log_storage,
    create_profile_storage,
    create_session_storage,
)
from ._memory import (
    RedisContinuityStorage,
    RedisProfileStorage,
    RedisSessionStorage,
    ShortMemoryStorage,
)
from ._pool import RedisPool, create_redis_pool


__all__ = [
    # Protocols / types
    "RedisLike",
    "PipelineLike",
    "EventLogStorage",
    "RedisEventLogAdapter",
    "RedisPool",
    "ShortMemoryStorage",
    "RedisSessionStorage",
    "RedisProfileStorage",
    "RedisContinuityStorage",
    "DLQStorage",
    "RedisDLQStorage",
    # Errors
    "RedisAdapterError",
    "RedisUnavailableError",
    "IdempotencyConflict",
    # Codec
    "decode_value",
    "decode_dict",
    "decode_int_dict",
    # Keys
    "AGENT_STREAM_KEY",
    "EVENT_ID_INDEX",
    "SCAN_PATTERN",
    "MAXLEN_DEFAULT",
    "stream_key_for_agent",
    "event_id_key",
    "parse_agent_id_from_stream_key",
    # Idempotency
    "claim_event_id_slot",
    # Factories
    "create_redis_pool",
    "create_event_log_storage",
    "create_session_storage",
    "create_profile_storage",
    "create_continuity_storage",
    "create_dlq_storage",
]
