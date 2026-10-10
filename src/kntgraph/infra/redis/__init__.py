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
- :mod:`._tools`         — tool-queue key conventions (ADR-076)

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
    MAXLEN_DEFAULT,
    SCAN_PATTERN,
    EventLogStorage,
    RedisEventLogAdapter,
    event_id_key,
    parse_agent_id_from_stream_key,
    stream_key_for_agent,
)
from ._event_log._idempotency import claim_event_id_slot
from ._factory import (
    create_api_key_storage,
    create_continuity_storage,
    create_dlq_storage,
    create_event_log_storage,
    create_profile_storage,
    create_session_storage,
    create_solution_storage,
)
from ._memory import (
    SOLUTION_KEY_PREFIX,
    RedisContinuityStorage,
    RedisProfileStorage,
    RedisSessionStorage,
    RedisSolutionStore,
    ShortMemoryStorage,
    SolutionStoreDecodeError,
    SolutionStoreError,
    SolutionStoreSerializationError,
)
from ._pool import RedisPool, create_redis_pool
from ._tools import TOOL_QUEUE_KEY_TEMPLATE, tool_queue_key

__all__ = [
    # Keys
    "AGENT_STREAM_KEY",
    "EVENT_ID_INDEX",
    "MAXLEN_DEFAULT",
    "SCAN_PATTERN",
    "SOLUTION_KEY_PREFIX",
    "TOOL_QUEUE_KEY_TEMPLATE",
    "DLQStorage",
    "EventLogStorage",
    "IdempotencyConflict",
    "PipelineLike",
    # Errors
    "RedisAdapterError",
    "RedisContinuityStorage",
    "RedisDLQStorage",
    "RedisEventLogAdapter",
    # Protocols / types
    "RedisLike",
    "RedisPool",
    "RedisProfileStorage",
    "RedisSessionStorage",
    "RedisSolutionStore",
    "RedisUnavailableError",
    "ShortMemoryStorage",
    "SolutionStoreDecodeError",
    "SolutionStoreError",
    "SolutionStoreSerializationError",
    # Idempotency
    "claim_event_id_slot",
    "create_api_key_storage",
    "create_continuity_storage",
    "create_dlq_storage",
    "create_event_log_storage",
    "create_profile_storage",
    # Factories
    "create_redis_pool",
    "create_session_storage",
    "create_solution_storage",
    "decode_dict",
    "decode_int_dict",
    # Codec
    "decode_value",
    "event_id_key",
    "parse_agent_id_from_stream_key",
    "stream_key_for_agent",
    "tool_queue_key",
]
