# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Private helpers for the ``WorkerManager`` queue lifecycle.

The module owns recovery policy (ADR-085), Redis failure recovery, and the
small side effects used by the manager's dispatch methods. The manager keeps
public state and delegates implementation details here to keep its maintainability
index within the repository's complexity budget.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import warnings
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

import structlog

from kntgraph.core._typing import JsonValue
from kntgraph.core.clock import utcnow
from kntgraph.core.event import Event
from kntgraph.infra.redis._client import RedisLike
from kntgraph.infra.redis._transport import is_saturation_error, is_transport_error
from kntgraph.tools._message_handlers import (
    build_acl_denied_event,
    build_completion_event,
    build_failure_event,
    evaluate_acl,
    log_acl_denial,
    parse_request_payload,
    reconnect_redis_after_transport_error,
)
from kntgraph.tools._result import ToolResult

if TYPE_CHECKING:
    from kntgraph.tools.manager import WorkerManager

logger = structlog.get_logger()

_REAPER_BATCH = 10


def maybe_open_deprecated_pool(manager: WorkerManager) -> bool:
    """Open the deprecated internal executor pool when explicitly enabled."""
    if not manager._legacy_pool:
        return False
    from kntgraph.tools.executors import PoolDeprecationWarning

    max_workers = manager._compute_max_workers()
    manager._mp_context = multiprocessing.get_context("spawn")
    manager._pool = ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=manager._mp_context,
    )
    warnings.warn(
        (
            f"WorkerManager opened the internal ProcessPoolExecutor "
            f"(max_workers={max_workers}). Deprecated; removed in v0.19 "
            f"(ADR-078 §5). Register "
            f"executor_factory=ProcessPoolFactory(...) instead."
        ),
        PoolDeprecationWarning,
        stacklevel=4,
    )
    return True


async def reconnect_after_transport_error(redis: RedisLike) -> None:
    """Reconnect the Redis client after a transport failure."""
    await reconnect_redis_after_transport_error(redis)


async def back_off_on_saturation(manager: WorkerManager, tool_name: str) -> None:
    """Delay briefly when the Redis pool is exhausted."""
    delay = manager._saturation_backoff_seconds
    logger.warning(
        "worker.pool_saturated",
        tool=tool_name,
        backoff_seconds=delay,
    )
    await asyncio.sleep(delay)
    from kntgraph.tools._message_handlers import next_saturation_backoff

    manager._saturation_backoff_seconds = next_saturation_backoff(
        delay, manager._saturation_backoff_ceiling_seconds
    )


async def handle_consume_error(
    manager: WorkerManager,
    tool_name: str,
    error: Exception,
) -> None:
    """Recover from a consume-loop failure according to its error class."""
    if is_saturation_error(error):
        await back_off_on_saturation(manager, tool_name)
        return
    if is_transport_error(error):
        await reconnect_after_transport_error(manager._redis)
    await asyncio.sleep(1)
    manager._maybe_emit_heartbeat(tool_name)


def mark_in_flight(manager: WorkerManager, tool_name: str, message_id: str) -> None:
    """Record that a message is currently executing."""
    manager._in_flight.setdefault(tool_name, set()).add(message_id)


def clear_in_flight(manager: WorkerManager, tool_name: str, message_id: str) -> None:
    """Drop the in-flight marker for a message."""
    ids = manager._in_flight.get(tool_name)
    if ids is not None:
        ids.discard(message_id)


def is_in_flight(manager: WorkerManager, tool_name: str, message_id: str) -> bool:
    """Return whether a message is currently executing."""
    return message_id in manager._in_flight.get(tool_name, ())


def tool_is_idempotent(manager: WorkerManager, tool_name: str) -> bool:
    """Return whether repeating a tool's side effects is safe."""
    tool_cls = manager._tools.get(tool_name)
    if tool_cls is None:
        return True
    return bool(getattr(tool_cls, "__tool_worker_idempotent__", True))


async def parse_message_payload(
    manager: WorkerManager,
    tool_name: str,
    message_id: str,
    message_data: dict,
    stream_key: str,
) -> Event | None:
    """Decode a request event, acknowledging malformed payloads."""
    request_event = parse_request_payload(message_data)
    if request_event is not None:
        return request_event
    logger.exception(
        "worker.payload_parse.error",
        tool=tool_name,
        message_id=message_id,
        error="see _message_handlers._log_payload_parse_error",
    )
    await manager._redis.xack(stream_key, manager._group_name, message_id)
    manager._messages_failed_total += 1
    return None


def evaluate_tool_acl(
    manager: WorkerManager,
    tool_name: str,
    request_event: Event,
) -> str | None:
    """Return the ACL denial reason, if any."""
    return evaluate_acl(
        tool_name=tool_name,
        request_event=request_event,
        acl_for=manager.acl_for,
    )


async def emit_acl_denied(
    manager: WorkerManager,
    tool_name: str,
    stream_key: str,
    message_id: str,
    idempotency_key: str,
    request_event: Event,
    denied_reason: str,
) -> None:
    """Publish an ACL denial, ACK it, and count the failure."""
    principal_id = request_event.producer_principal_id
    log_acl_denial(
        tool_name=tool_name,
        message_id=message_id,
        denied_reason=denied_reason,
        principal_id=principal_id,
    )
    denied_event = build_acl_denied_event(
        tool_name=tool_name,
        request_event=request_event,
        idempotency_key=idempotency_key,
        denied_reason=denied_reason,
        principal_id=principal_id,
    )
    await manager._event_log.append(denied_event)
    await manager._redis.xack(stream_key, manager._group_name, message_id)
    manager._messages_failed_total += 1


async def translate_completion_to_event(
    manager: WorkerManager,
    tool_name: str,
    request_event: Event,
    idempotency_key: str,
    result: ToolResult,
) -> None:
    """Publish the event corresponding to a tool result."""
    if result.status == "ok":
        event = build_completion_event(
            tool_name=tool_name,
            request_event=request_event,
            idempotency_key=idempotency_key,
            result=result,
        )
        await manager._event_log.append(event)
        manager._messages_processed_total += 1
        return
    event = build_failure_event(
        tool_name=tool_name,
        request_event=request_event,
        idempotency_key=idempotency_key,
        result=result,
    )
    await manager._event_log.append(event)
    manager._messages_failed_total += 1


async def dispatch_tool(
    manager: WorkerManager,
    tool_name: str,
    tool_cls: type,
    idempotency_key: str,
    tool_params: Mapping[str, JsonValue],
) -> ToolResult:
    """Dispatch a request through the manager's execution strategy."""
    from kntgraph.tools._dispatch import dispatch_to_tool

    return await dispatch_to_tool(
        tool_name,
        tool_cls,
        idempotency_key,
        tool_params,
        executors=manager._executors,
        pool=manager._pool,
    )


async def pending_payload(redis: RedisLike, stream_key: str, msg_id: str) -> dict:
    """Return the field map for one pending stream entry."""
    rows = await redis.xrange(stream_key, min=msg_id, max=msg_id, count=1)
    if not rows:
        return {}
    return dict(rows[0][1])


def reclaim_threshold_ms(
    payload: Mapping[str, object],
    fallback_ms: int,
    *,
    timeout_seconds: float | None = None,
) -> int:
    """Return the reclaim threshold for one message in milliseconds.

    The threshold is the time the message may still wait. It is
    derived from the **tool's** declared timeout and the
    request's own timestamp:

        requested_at + timeout − now

    ``timeout_seconds`` is the tool's declaration — ``None`` when
    the tool declared none, or when the payload carries no usable
    timestamp. Either way the caller falls back to the global
    ``reaper_idle_time``.

    An expired deadline yields ``0`` — reclaimable immediately.
    """
    from kntgraph.tools._ttl import read_requested_at

    if timeout_seconds is None:
        return fallback_ms
    requested_at = read_requested_at(payload)
    if requested_at is None:
        return fallback_ms
    elapsed = (utcnow() - requested_at).total_seconds()
    remaining = timeout_seconds - elapsed
    return max(0, int(remaining * 1000))


async def emit_non_idempotent_failure(
    manager: WorkerManager,
    *,
    tool_name: str,
    stream_key: str,
    message_id: str,
    idempotency_key: str,
    request_event: Event,
) -> None:
    """Publish the permanent failure, ACK the request, and count it."""
    from kntgraph.tools._message_handlers import build_non_idempotent_failure_event

    failed_event = build_non_idempotent_failure_event(
        tool_name=tool_name,
        request_event=request_event,
        idempotency_key=idempotency_key,
        principal_id=request_event.producer_principal_id,
    )
    await manager._event_log.append(failed_event)
    await manager._redis.xack(stream_key, manager._group_name, message_id)
    manager._messages_failed_total += 1


async def resolve_non_idempotent_claim(
    manager: WorkerManager,
    *,
    tool_name: str,
    stream_key: str,
    message_id: str,
    payload: dict,
) -> None:
    """Resolve a reclaimed non-idempotent request without re-running it."""
    request_event = await parse_message_payload(
        manager, tool_name, message_id, payload, stream_key
    )
    if request_event is None:
        return
    await emit_non_idempotent_failure(
        manager,
        tool_name=tool_name,
        stream_key=stream_key,
        message_id=message_id,
        idempotency_key=str(request_event.event_id),
        request_event=request_event,
    )


def _decode_message_id(raw_id: bytes | str) -> str:
    """Return the textual Redis Stream identifier."""
    return raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)


async def reaper_loop_once(manager: WorkerManager, tool_name: str) -> None:
    """Inspect and recover the tool's pending messages once."""
    stream_key = manager._stream_key(tool_name)
    fallback_idle_ms = int(manager._reaper_idle_time * 1000)
    pending = await manager._redis.xpending_range(
        stream_key,
        manager._group_name,
        min="-",
        max="+",
        count=_REAPER_BATCH,
    )
    if not pending:
        return

    semaphore = manager._semaphores.get(tool_name)
    # The tool's own latency budget (ADR-085 §3.2). The manager
    # holds it because it registered the tool; the reaper runs
    # inside the manager. Nothing has to be published for the
    # deadline to be known.
    timeout_seconds = manager.published_timeout_for(tool_name)
    for entry in pending:
        message_id = _decode_message_id(entry["message_id"])
        idle_ms = int(entry.get("time_since_delivered") or 0)
        payload = await pending_payload(manager._redis, stream_key, message_id)
        threshold_ms = reclaim_threshold_ms(
            payload,
            fallback_idle_ms,
            timeout_seconds=timeout_seconds,
        )
        if idle_ms < threshold_ms:
            continue
        if is_in_flight(manager, tool_name, message_id):
            logger.warning(
                "worker.reaper.skipped_in_flight",
                tool=tool_name,
                message_id=message_id,
            )
            continue
        if not tool_is_idempotent(manager, tool_name):
            logger.warning(
                "worker.reaper.non_idempotent_not_rerun",
                tool=tool_name,
                message_id=message_id,
            )
            await resolve_non_idempotent_claim(
                manager,
                tool_name=tool_name,
                stream_key=stream_key,
                message_id=message_id,
                payload=payload,
            )
            continue

        claimed = await manager._redis.xautoclaim(
            name=stream_key,
            groupname=manager._group_name,
            consumername=manager._consumer_name,
            min_idle_time=threshold_ms,
            start_id="0-0",
            count=1,
        )
        messages = claimed[1] if len(claimed) > 1 else []
        if not messages:
            continue

        logger.warning(
            "worker.reaper.reclaimed",
            tool=tool_name,
            message_id=message_id,
        )
        asyncio.create_task(
            _process_claimed_message(
                manager,
                tool_name=tool_name,
                stream_key=stream_key,
                message_id=message_id,
                message_data=messages[0][1],
                semaphore=semaphore,
            )
        )


async def _process_claimed_message(
    manager: WorkerManager,
    *,
    tool_name: str,
    stream_key: str,
    message_id: str,
    message_data: dict,
    semaphore: asyncio.Semaphore | None,
) -> None:
    """Process a claimed message while respecting the concurrency cap."""
    if semaphore is not None:
        async with semaphore:
            await manager._process_message(
                tool_name, stream_key, message_id, message_data
            )
        return
    await manager._process_message(tool_name, stream_key, message_id, message_data)
