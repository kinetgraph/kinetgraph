# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis world-checkpoint adapter — sub-package re-exports.

Public API
----------

- :class:`WorldCheckpointStorage` — domain Protocol
- :class:`RedisWorldCheckpointStorage` — Redis implementation
- :const:`WORLD_CHECKPOINT_KEY_TEMPLATE`
"""

from ._adapter import WorldCheckpointStorage
from ._redis import (
    RedisWorldCheckpointStorage,
    WORLD_CHECKPOINT_KEY_TEMPLATE,
    storage_key,
)


__all__ = [
    "RedisWorldCheckpointStorage",
    "WORLD_CHECKPOINT_KEY_TEMPLATE",
    "WorldCheckpointStorage",
    "storage_key",
]
