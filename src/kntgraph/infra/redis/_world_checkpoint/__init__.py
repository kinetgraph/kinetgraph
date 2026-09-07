# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis world-checkpoint adapter — sub-package re-exports.

Public API
----------

- :class:`WorldCheckpointStorage` — domain Protocol
- :class:`RedisWorldCheckpointStorage` — Redis implementation
- :const:`WORLD_CHECKPOINT_KEY_TEMPLATE` — checkpoint payload key
- :const:`WORLD_CURSOR_KEY_TEMPLATE` — cursor key (P5b split)
- :func:`storage_key` / :func:`cursor_key` — key helpers
"""

from ._adapter import WorldCheckpointStorage
from ._redis import (
    RedisWorldCheckpointStorage,
    WORLD_CHECKPOINT_KEY_TEMPLATE,
    WORLD_CURSOR_KEY_TEMPLATE,
    cursor_key,
    storage_key,
)


__all__ = [
    "RedisWorldCheckpointStorage",
    "WORLD_CHECKPOINT_KEY_TEMPLATE",
    "WORLD_CURSOR_KEY_TEMPLATE",
    "WorldCheckpointStorage",
    "cursor_key",
    "storage_key",
]
