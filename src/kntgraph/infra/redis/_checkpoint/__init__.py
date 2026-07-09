# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis checkpoint adapter — sub-package re-exports.

Public API
----------

- :class:`CheckpointStorage`        — domain Protocol
- :class:`RedisCheckpointStorage`    — Redis implementation
- :const:`CHECKPOINT_KEY`            — Redis key convention
"""

from ._adapter import CheckpointStorage
from ._redis import CHECKPOINT_KEY, RedisCheckpointStorage


__all__ = [
    "CHECKPOINT_KEY",
    "CheckpointStorage",
    "RedisCheckpointStorage",
]
