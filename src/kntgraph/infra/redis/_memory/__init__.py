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
"""

from ._adapter import ShortMemoryStorage
from ._continuity import RedisContinuityStorage
from ._profile import RedisProfileStorage
from ._session import RedisSessionStorage


__all__ = [
    "ShortMemoryStorage",
    "RedisContinuityStorage",
    "RedisProfileStorage",
    "RedisSessionStorage",
]
