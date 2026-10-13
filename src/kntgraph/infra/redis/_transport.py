# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Transport-layer failure classifier (ADR-082 §3.1).

The framework catches transport failures at two seams:

  - the storage-adapter boundary (ADR-077): the third-party
    ``redis-py`` exception is translated to
    ``RedisUnavailableError`` (or to ``PersistenceError``
    for ``EventLog.append``).
  - the consumer boundary (this ADR): the long-running
    ``_consume_loop`` and ``_wake_once`` paths catch the
    same exception classes and trigger a proactive
    ``close_transport()`` + ``pool.disconnect()``.

The predicate is the single source of truth for "is this a
transport-class failure?". It is class-based, never
substring-based, because substring matching on exception
messages is implicit duck typing on a string the framework
does not own and the ``redis-py`` library is free to change.

Application-level ``RedisError`` subclasses (``WatchError``,
``ResponseError``, ``DataError``) are excluded by design —
those indicate a wrong command, not a transport failure
(ADR-077 §4.3).
"""

from __future__ import annotations

import redis.exceptions as redis_exceptions

_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    redis_exceptions.ConnectionError,
    redis_exceptions.TimeoutError,
    redis_exceptions.BusyLoadingError,
    ConnectionError,
    TimeoutError,
    OSError,
    # redis-py uses ``RuntimeError("Transport is closed")``
    # for transport-class state changes (the post-mortem
    # of 2026-10-07 identified this exact pattern). The
    # class is generic, but the message body is the
    # stable contract redis-py ships; including the class
    # catches every variant redis-py emits.
    RuntimeError,
)


def is_transport_error(e: BaseException) -> bool:
    """True when ``e`` is a transport-layer failure that
    requires proactive reconnect.

    The match is class-based; ``redis-py`` exception classes
    are stable across the 4.x and 5.x lines.

    ``RuntimeError`` is included because ``redis-py`` raises
    ``RuntimeError("Transport is closed")`` for transport-
    class state changes — a stable pattern that the
    2026-10-07 post-mortem identified. Application-level
    ``RedisError`` subclasses (``WatchError``,
    ``ResponseError``, ``DataError``) are excluded by
    design — those indicate a wrong command, not a
    transport failure (ADR-077 §4.3).
    """
    return isinstance(e, _TRANSPORT_ERRORS)


__all__ = ["is_transport_error"]
