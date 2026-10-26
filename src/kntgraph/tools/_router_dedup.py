# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
The tool-queue dedup path (ADR-083 §3.2).

Extracted from ``tools/router.py`` so the router stays small
(the radon MI gate): the dedup flow is self-contained — it
claims the idempotency slot, decides whether the write was a
hit, a transport failure, or a success, and logs the outcome.

The router keeps the orchestration (``route_batch``) and the
key composition; this module owns the "what did the claim
mean?" decision.
"""

from __future__ import annotations

import logging

from kntgraph.core.result import PersistenceError
from kntgraph.infra.redis._client import RedisLike
from kntgraph.infra.redis._tools import claim_tool_queue_slot

logger = logging.getLogger(__name__)

# The marker the dedup helper uses to report "this event_id was
# already routed" (ADR-083 §3.2).
_DEDUP_HIT_MARKER = "tool_queue_dedup_hit"


async def route_with_dedup(
    *,
    redis: RedisLike,
    idem_key: str,
    stream_key: str,
    payload: dict,
    dedup_ttl_seconds: int,
    tool_name: str,
    event_id: str,
) -> bool:
    """Claim the idempotency slot and append the request.

    ``True`` when the event was handled (appended, or skipped
    because its ``event_id`` was already routed) — the caller
    must not call the bare ``xadd``. ``False`` on a
    transport-class error: the caller falls through to the bare
    ``xadd`` so a transient Redis hiccup never costs the
    dispatcher a real request.
    """
    result = await claim_tool_queue_slot(
        redis,
        idem_key=idem_key,
        stream_key=stream_key,
        payload=payload,
        maxlen=None,
        ttl_seconds=dedup_ttl_seconds,
    )
    if result.is_ok():
        logger.debug(
            f"worker.router.appended tool={tool_name} "
            f"event_id={event_id} stream_id={result.ok_value()}"
        )
        return True
    err = result.err_value()
    if isinstance(err, PersistenceError) and (_DEDUP_HIT_MARKER in str(err)):
        logger.debug(
            f"worker.router.skipped_dedup tool={tool_name} event_id={event_id}"
        )
        return True
    # Transport-class error -- caller falls through to bare xadd.
    logger.error(
        f"worker.router.dedup_error.falling_through_to_xadd "
        f"tool={tool_name} event_id={event_id} error={err}"
    )
    return False


__all__ = ["route_with_dedup"]
