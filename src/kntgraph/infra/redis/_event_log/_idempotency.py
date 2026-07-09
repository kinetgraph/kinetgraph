# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Idempotency helpers for the EventLog.

Decomposes the original two-phase write into three explicit
phases:

  1. :func:`_check_phase`   — read the idempotency index.
  2. :func:`_claim_phase`   — XADD + SET placeholder.
  3. :func:`_finalize_phase` — replace placeholder with stream_id.

The orchestrator :func:`claim_event_id_slot` composes them.
Each phase is a small, testable function (max ~15 LOC).

Why three phases
----------------

The stream id is only known AFTER ``XADD`` returns. A
single-statement pipeline (XADD + SET key stream_id) cannot
reference the result of XADD. The placeholder pattern is the
canonical solution: claim the key, defer the final value.

Crash safety
------------

A process crash between claim and finalize leaves the
placeholder in the index. The next reader treats this as a
concurrent-insert signal (``IdempotencyConflict``) — not a
silent re-append.
"""

from __future__ import annotations

from .._client import RedisLike
from .._codec import decode_value
from .._errors import IdempotencyConflict


PLACEHOLDER: str = "PLACEHOLDER"


def is_placeholder(value: str | None) -> bool:
    """True iff ``value`` is a non-final placeholder."""
    return value == PLACEHOLDER


async def _check_phase(
    redis: RedisLike,
    idem_key: str,
) -> str | None:
    """Phase 1: read the idempotency index.

    Returns
    -------
    - ``None`` if the key has never been claimed.
    - the stream_id (``str``) if a final value is present.
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
    maxlen: int,
    idem_key: str,
) -> str:
    """Phase 2: XADD + SET placeholder in one pipeline.

    Returns the stream_id returned by XADD. The placeholder
    is written under ``idem_key`` with ``nx=True`` so a
    concurrent claim does not overwrite an existing entry.
    """
    pipe = redis.pipeline(transaction=True)
    pipe.xadd(stream_key, payload, maxlen=maxlen)
    pipe.set(idem_key, PLACEHOLDER, nx=True)
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
) -> None:
    """Phase 3: replace the placeholder with the final stream_id."""
    await redis.set(idem_key, stream_id)


async def claim_event_id_slot(
    redis: RedisLike,
    idem_key: str,
    stream_key: str,
    payload: dict,
    maxlen: int,
) -> str:
    """Three-phase orchestrator. See module docstring."""
    existing = await _check_phase(redis, idem_key)
    if existing is not None:
        return existing
    stream_id = await _claim_phase(redis, stream_key, payload, maxlen, idem_key)
    await _finalize_phase(redis, idem_key, stream_id)
    return stream_id


async def read_final_id(
    redis: RedisLike,
    idem_key: str,
) -> str | None:
    """Read the SET-based idempotency index. Returns ``None`` if missing."""
    raw = await redis.get(idem_key)
    return decode_value(raw)


__all__ = [
    "PLACEHOLDER",
    "claim_event_id_slot",
    "is_placeholder",
    "read_final_id",
]
