# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis DLQ adapter — sub-package re-exports.

Public API
----------

- :class:`DLQStorage`               — domain Protocol
- :class:`RedisDLQStorage`           — Redis implementation
- Constants: ``DLQ_STREAM_KEY``, ``DLQ_REASON_INDEX``,
  ``DLQ_AGENT_INDEX``, ``DLQ_EVENT_INDEX``, ``PLACEHOLDER``,
  ``MAXLEN_DEFAULT``
- :func:`idem_key_for`               — idem_key builder
- :const:`ALL_KEYS`                  — tuple of all 4 keys
"""

from ._adapter import DLQStorage
from ._redis import (
    ALL_KEYS,
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
    DLQ_STREAM_KEY,
    MAXLEN_DEFAULT,
    PLACEHOLDER,
    RedisDLQStorage,
    idem_key_for,
)


__all__ = [
    "ALL_KEYS",
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "DLQStorage",
    "MAXLEN_DEFAULT",
    "PLACEHOLDER",
    "RedisDLQStorage",
    "idem_key_for",
]
