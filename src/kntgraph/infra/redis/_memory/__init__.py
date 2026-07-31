# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis short-memory adapter — sub-package re-exports.

Public API
----------

- :class:`ShortMemoryStorage`      — domain Protocol
- :class:`RedisSessionStorage`     — JSON-backed Session cache
- :class:`RedisProfileStorage`     — Hash-backed Profile cache
- :class:`RedisContinuityStorage`  — Hash-backed Continuity cache (sliding TTL)
- :class:`RedisSolutionStore`      — Hash-backed Solution cache (ADR-010 / ADR-049)
"""

from ._adapter import ShortMemoryStorage
from ._continuity import RedisContinuityStorage
from ._profile import RedisProfileStorage
from ._session import RedisSessionStorage
from ._solution import (
    RedisSolutionStore,
    SOLUTION_KEY_PREFIX,
    SolutionStoreDecodeError,
    SolutionStoreError,
    SolutionStoreSerializationError,
)


__all__ = [
    "RedisSolutionStore",
    "SOLUTION_KEY_PREFIX",
    "ShortMemoryStorage",
    "RedisContinuityStorage",
    "RedisProfileStorage",
    "RedisSessionStorage",
    "SolutionStoreDecodeError",
    "SolutionStoreError",
    "SolutionStoreSerializationError",
]
