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

# Pool exhaustion is NOT a transport failure (ADR-084 §2.4).
# redis-py raises this immediately — it does not queue — so it
# needs its own classification: the sockets are fine, there are
# simply not enough of them. Treating it as a transport failure
# makes the consumer tear the pool down, which kills every
# sibling consumer's socket and turns a momentary burst into a
# reconnect storm (ADR-084 §1.5).
SATURATION_ERRORS: tuple[type[BaseException], ...] = (
    redis_exceptions.MaxConnectionsError,
)

TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
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


def is_saturation_error(e: BaseException) -> bool:
    """True when ``e`` is a pool-exhaustion failure.

    ``MaxConnectionsError`` inherits from redis-py's
    ``ConnectionError``, so ``is_transport_error`` would
    otherwise match it. The two need different responses:
    saturation calls for back-pressure, a broken transport calls
    for a reconnect (ADR-084 §2.4, §3.4).
    """
    return isinstance(e, SATURATION_ERRORS)


def _effective_transport_errors(e: BaseException) -> tuple[type[BaseException], ...]:
    """The transport classes that still apply to ``e``.

    Pool saturation is excluded (ADR-084 §2.4): the sockets are
    healthy, so reconnecting does not help — it makes it worse.
    A saturated exception therefore matches no transport class
    at all, and the caller — :func:`is_transport_error` — needs
    only a single membership test to classify it.

    The exclusion is per-exception rather than a static filter on
    ``TRANSPORT_ERRORS`` because ``MaxConnectionsError`` is a
    *subclass* of redis-py's ``ConnectionError``: dropping the
    parent from the tuple would also drop every genuine transport
    failure that shares it.
    """
    if is_saturation_error(e):
        return ()
    return TRANSPORT_ERRORS


def is_transport_error(e: BaseException) -> bool:
    """True when ``e`` is a transport-class failure that
    requires proactive reconnect (ADR-082 §3.1).

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

    Pool saturation is excluded (ADR-084 §2.4): the sockets are
    healthy, so reconnecting does not help — it makes it worse.
    The classification stays a single membership test, so the
    predicate has one decision point.
    """
    return isinstance(e, _effective_transport_errors(e))


__all__ = [
    "SATURATION_ERRORS",
    "TRANSPORT_ERRORS",
    "is_saturation_error",
    "is_transport_error",
]
