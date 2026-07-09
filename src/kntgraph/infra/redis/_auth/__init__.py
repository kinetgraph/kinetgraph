# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis auth adapter — sub-package re-exports.

Public API
----------

- :class:`APIKeyStorage`        — domain Protocol
- :class:`RedisAPIKeyStorage`    — Redis implementation (bytes-in-bytes-out)
- :class:`APIKeyCacheAdapter`    — in-process TTL cache (Iter 17b)
- :const:`KEY_PREFIX`            — Redis key prefix convention
"""

from ._adapter import APIKeyStorage
from ._cache import APIKeyCacheAdapter
from ._redis import KEY_PREFIX, RedisAPIKeyStorage, storage_key


__all__ = [
    "APIKeyCacheAdapter",
    "APIKeyStorage",
    "KEY_PREFIX",
    "RedisAPIKeyStorage",
    "storage_key",
]
