# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Idempotency helpers for the per-tool Redis Stream queue
(ADR-083).

Mirrors the three-phase pattern of
:mod:`kntgraph.infra.redis._event_log._idempotency` so the
codebase has a single shape for "Redis Streams write with
dedup". The two helpers differ in the **key** they read
(EventLog uses ``knt:eventids:<id>``; tool queues use
``knt:tools:<tool>:idem:<id>`` per ADR-083 §3.1) and the
**error they raise on dedup hit** (the EventLog raises
``IdempotencyConflict``; the tool queue wraps it in
``Err(PersistenceError("tool_queue_dedup_hit"))`` because
the caller wants a single, distinguishable success/skip
signal).

Three phases
------------

  1. :func:`_check_phase`    -- read the dedup index.
  2. :func:`_claim_phase`    -- ``XADD`` + ``SET`` placeholder (NX, EX)
     in one pipeline.
  3. :func:`_finalize_phase` -- replace placeholder with the
     final stream_id.

The orchestrator :func:`claim_tool_queue_slot` composes
them. The dedup hit (``Err(PersistenceError("tool_queue_dedup_hit"))``)
is the desired outcome of a duplicate ``XADD``; the
``ToolRouter.route_batch`` caller maps it to
``worker.router.skipped_dedup`` and continues (no
re-raise). A transport error (``Err(PersistenceError("redis: ..."))``)
spans the caller's existing ``except`` clause in
``route_batch``; the dispatcher must not drop a request on
a transient Redis hiccup (the fall-through behaviour in
ADR-083 §3.3).

Crash safety
------------

A process crash between claim and finalize leaves the
placeholder in the index. The next reader treats this as
a concurrent-insert signal -- not a silent re-append.
The TTL on the placeholder (``ex=ttl_seconds``) bounds
the in-flight window.
"""

from __future__ import annotations

import structlog
from redis import exceptions as redis_exceptions

from kntgraph.core.result import Err, Ok, PersistenceError, Result

from .._client import RedisLike
from .._codec import decode_value
from .._errors import IdempotencyConflict

logger = structlog.get_logger()

PLACEHOLDER: str = "PLACEHOLDER"

PLACEHOLDER_TTL_SECONDS: int = 60
"""TTL of the in-flight placeholder (60 seconds, mirrors
:mod:`kntgraph.infra.redis._event_log._idempotency._claim_phase`).

Bounded to the round-trip window of a single ``XADD + SET``
pipeline. If the writer dies between phase 2 and phase 3,
the placeholder expires and the next reader re-runs the
path. The operator-tunable TTL applies to the final
``stream_id`` value stored in phase 3.
"""


def is_placeholder(value: str | None) -> bool:
    """True iff ``value`` is a non-final placeholder."""
    return value == PLACEHOLDER


async def _check_phase(
    redis: RedisLike,
    idem_key: str,
) -> str | None:
    """Phase 1: read the dedup index.

    Returns
    -------
    - ``None`` if the key has never been claimed.
    - the ``stream_id`` (``str``) if a final value is present.
    - raises :class:`IdempotencyConflict` if a concurrent
      writer holds the placeholder.
    """
    existing = await redis.get(idem_key)
    if existing is None:
        return None
    decoded = decode_value(existing)
    if decoded is None or is_placeholder(decoded):
        raise IdempotencyConflict(idem_key)
    return decoded


async def _claim_phase(
    redis: RedisLike,
    stream_key: str,
    payload: dict,
    maxlen: int | None,
    idem_key: str,
) -> str:
    """Phase 2: ``XADD`` + ``SET`` placeholder in one pipeline.

    Returns the ``stream_id`` returned by ``XADD``. The
    placeholder is written under ``idem_key`` with
    ``nx=True`` so a concurrent claim does not overwrite an
    existing entry. The placeholder TTL is hardcoded to 60
    seconds (mirrors
    :mod:`kntgraph.infra.redis._event_log._idempotency._claim_phase`)
    because the placeholder is just an in-flight marker --
    the operator-tunable TTL applies to the final
    ``stream_id`` value stored in phase 3.
    """
    pipe = redis.pipeline(transaction=True)
    pipe.xadd(stream_key, payload, maxlen=maxlen)
    pipe.set(idem_key, PLACEHOLDER, nx=True, ex=PLACEHOLDER_TTL_SECONDS)
    results = await pipe.execute()

    stream_id = decode_value(results[0])
    if stream_id is None:
        # Unreachable under the redis-py asyncio client;
        # documents the contract for future maintainers.
        raise RuntimeError(
            f"XADD returned None for idem_key={idem_key!r}; "
            "this is unreachable under the redis-py asyncio "
            "client and would indicate a library contract change."
        )
    return stream_id


async def _finalize_phase(
    redis: RedisLike,
    idem_key: str,
    stream_id: str,
    ttl_seconds: int,
) -> None:
    """Phase 3: replace the placeholder with the final ``stream_id``.

    The TTL matches the placeholder's TTL so the dedup
    index entry expires ``ttl_seconds`` after the original
    write -- long enough to cover any sane reactive
    re-emit window, short enough to bound the index size.
    """
    if ttl_seconds > 0:
        await redis.set(idem_key, stream_id, ex=ttl_seconds)
    else:
        await redis.set(idem_key, stream_id)


async def claim_tool_queue_slot(
    redis: RedisLike,
    *,
    idem_key: str,
    stream_key: str,
    payload: dict,
    maxlen: int | None,
    ttl_seconds: int,
) -> Result[str, PersistenceError]:
    """Three-phase orchestrator for a deduped ``XADD`` to a tool queue.

    Returns
    -------
    - ``Ok(stream_id)`` -- the new entry's stream id (first
      write for this ``event_id``).
    - ``Err(PersistenceError("tool_queue_dedup_hit"))`` --
      the ``event_id`` was already in the queue (final
      stream_id) or a concurrent writer holds the
      placeholder. The caller (``ToolRouter.route_batch``)
      treats this as a silent skip and logs
      ``worker.router.skipped_dedup`` -- it is the desired
      outcome of a duplicate re-emit, not an error.
    - ``Err(PersistenceError("redis: ..."))`` -- transport
      error (``RedisError``, ``ConnectionError``,
      ``TimeoutError``, ``OSError``) at any of the three
      phases. The caller falls through to the bare
      ``xadd`` so a transient Redis hiccup never costs the
      dispatcher a real request (ADR-083 §3.3
      "best-effort fan-out, never drop").

    Why three phases instead of ``EXISTS + SET NX``
    ------------------------------------------------

    The EventLog helper uses the same pattern (ADR-019
    §3.2). The placeholder lets the **caller distinguish**
    "first write" from "concurrent write in flight" from
    "duplicate". A two-phase ``EXISTS + SET`` cannot
    distinguish these (both "first write" and "in flight"
    return ``False``); the caller would have to choose
    between "retry on every wake" (re-emit storm) and
    "skip on every wake" (lost messages). The
    placeholder is the only correct shape.

    Cost: one extra Redis ``SET`` per write. At the
    tool-queue write rate (a few writes per second per
    tool even in the backoffice scenario), this is
    irrelevant.
    """

    try:
        existing = await _check_phase(redis, idem_key)
    except IdempotencyConflict:
        # A concurrent writer holds the placeholder.
        # The ToolRouter caller treats this as a skip
        # (consistent with the finalised-id branch).
        return Err(PersistenceError("tool_queue_dedup_hit"))
    except (
        redis_exceptions.RedisError,
        ConnectionError,
        TimeoutError,
        OSError,
    ) as exc:
        logger.warning(
            "tool_queue.idempotency.check_phase.transport_error",
            idem_key=idem_key,
            error=str(exc),
        )
        return Err(PersistenceError(f"redis: {type(exc).__name__}"))
    if existing is not None:
        return Err(PersistenceError("tool_queue_dedup_hit"))
    try:
        stream_id = await _claim_phase(redis, stream_key, payload, maxlen, idem_key)
    except (
        redis_exceptions.RedisError,
        ConnectionError,
        TimeoutError,
        OSError,
    ) as exc:
        logger.warning(
            "tool_queue.idempotency.claim_phase.transport_error",
            idem_key=idem_key,
            error=str(exc),
        )
        return Err(PersistenceError(f"redis: {type(exc).__name__}"))
    try:
        await _finalize_phase(redis, idem_key, stream_id, ttl_seconds)
    except (
        redis_exceptions.RedisError,
        ConnectionError,
        TimeoutError,
        OSError,
    ) as exc:
        # The XADD already succeeded (the stream entry exists).
        # The dedup marker is just a hint for future readers;
        # a failed finalize means the next read will re-XADD
        # the same event_id (a duplicate the caller will see
        # as a duplicate on the SECOND write, but the FIRST
        # write succeeded). Falling through is the right
        # behaviour: the caller already has a stream entry.
        logger.warning(
            "tool_queue.idempotency.finalize_phase.transport_error",
            idem_key=idem_key,
            stream_id=stream_id,
            error=str(exc),
        )
        return Err(PersistenceError(f"redis: {type(exc).__name__}"))
    return Ok(stream_id)


__all__ = [
    "PLACEHOLDER",
    "claim_tool_queue_slot",
    "is_placeholder",
]
