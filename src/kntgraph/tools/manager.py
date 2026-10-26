# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Worker Manager for the Tool Worker Pattern (ADR-036).

The manager owns queue consumption, ACL enforcement, tool execution, event
publication, and pending-message recovery. ADR-085 recovery logic lives in
``kntgraph.tools._reaper``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.context import BaseContext

    from kntgraph.infra.redis import RedisLike

import structlog

from kntgraph.core._typing import JsonValue
from kntgraph.core.event import Event
from kntgraph.infra.redis._prefix import validate_prefix
from kntgraph.infra.redis._tools import tool_queue_key
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools._dispatch import ExecutorFactory
from kntgraph.tools._dispatch_helpers import (
    compute_max_workers,
)
from kntgraph.tools._message_handlers import (
    coerce_tool_params,
    escalate_on_retry_budget_exceeded,
)
from kntgraph.tools._reaper import (
    back_off_on_saturation,
    clear_in_flight,
    dispatch_tool,
    emit_acl_denied,
    evaluate_tool_acl,
    handle_consume_error,
    is_in_flight,
    mark_in_flight,
    maybe_open_deprecated_pool,
    parse_message_payload,
    pending_payload,
    reaper_loop_once,
    reclaim_threshold_ms,
    reconnect_after_transport_error,
    translate_completion_to_event,
)
from kntgraph.tools._result import ToolResult
from kntgraph.tools._strategy_gate import reject_undeclared_sync_tool
from kntgraph.tools._worker_invocation import _invoke_tool_sync
from kntgraph.tools.acl import ToolACL, default_acl
from kntgraph.tools.descriptors import ToolDescriptor, schema_to_json

_ORIGINAL_INVOKE_TOOL_SYNC = _invoke_tool_sync

logger = structlog.get_logger()

# Sentinel used to differentiate "the caller did not pass ``acl=``" (legacy, no
# constraint) from "the caller passed ``acl=None`` explicitly" (the new
# default-allow-with-no-policy contract). The legacy path is preserved at the API
# level: omitting ``acl=`` keeps the pre-v0.16 behaviour. Passing ``acl=None`` is
# reserved for future use (e.g. the v0.17 step flips the default to
# ``default_acl()`` and uses this sentinel to detect the explicit opt out).
_UNSET: object = object()


__all__ = [
    "ToolACL",
    "ToolDescriptor",
    "WorkerManager",
    "_invoke_tool_sync",
    "default_acl",
]


_SPAWN_METHOD = "spawn"

# Pool-saturation back-off (ADR-084 §3.4). The first saturation waits this long; each
# consecutive one doubles, up to the ceiling. Short enough that a brief burst is
# barely noticeable, long enough that a sustained overload does not spin.
_SATURATION_BACKOFF_SECONDS = 0.25
_SATURATION_BACKOFF_CEILING_SECONDS = 2.0


class WorkerManager:
    """
    Manages the lifecycle of Tool Workers.
    Listens to Redis Streams (via Consumer Groups) and delegates execution
    to a ProcessPoolExecutor to avoid blocking the main event loop.
    """

    def __init__(
        self,
        redis: RedisLike,
        event_log: EventLog,
        group_name: str = "fmh_tool_workers",
        consumer_name: str = "worker-1",
        reaper_interval: float = 60.0,
        reaper_idle_time: float = 300.0,
        heartbeat_interval_seconds: float = 30.0,
        *,
        key_prefix: str = "",
        max_pool_workers: int | None = None,
        legacy_pool: bool = False,
    ):
        # ADR-076 / DEBT §2.35: namespace prefix for every Redis key the manager
        # writes or reads (``knt:tools:<tool>:queue`` -- the consumer group
        # creation, the consume loop, and the reaper loop all flow through
        # ``_stream_key``). Empty string (default) is byte-for-byte identical to the
        # pre-076 wire format.
        validate_prefix(key_prefix)
        self._redis = redis
        self._event_log = event_log
        self._group_name = group_name
        self._consumer_name = consumer_name
        self._key_prefix = key_prefix

        self._reaper_interval = reaper_interval
        self._reaper_idle_time = reaper_idle_time
        self._tools: dict[str, type] = {}
        self._pool: ProcessPoolExecutor | None = None
        # Cached in ``start()``; stored here so tests can assert on it
        # without re-deriving the default.
        self._mp_context: BaseContext | None = None

        # Per-tool executor factory (ADR-069). When ``None`` for a given tool, the
        # Manager falls back to its internal ``ProcessPoolExecutor``. When set, the
        # factory is called once per message in place of ``run_in_executor``. The
        # Manager remains the owner of XACK / events / reaper / ACL /
        # observability; the factory is a black-box dispatch hook.
        self._executors: dict[str, ExecutorFactory] = {}
        # Explicit cap on the internal ``ProcessPoolExecutor``. ``None`` (default)
        # means "use the legacy formula ``max(1, sum(max_concurrency))``"; an
        # integer means "use exactly N workers regardless of the number of tools".
        # In Fargate-class environments with memory caps, this is the lever that
        # prevents the OOM of the post-mortem.
        self._max_pool_workers: int | None = max_pool_workers
        # ADR-078 §4.4: opt-in to the deprecated internal pool. ``False`` (default)
        # means ``start()`` creates no pool at all and tools must declare their own
        # strategy via ``register(executor_factory=...)``. ``max_pool_workers``
        # sizes the pool when this flag is set; it does not create one.
        self._legacy_pool = legacy_pool
        self._deprecated_pool_opened = False

        # Per-tool ``asyncio.Semaphore`` (``max_concurrency``) — populated in
        # ``_consume_loop`` and consumed by the ``_reaper_loop`` to honour the
        # per-tool concurrency limit. Without it the reaper bypasses the semaphore
        # and fires N parallel jobs → OOM where the workers hold heavy ML models.
        # post-mortem 2026-10-07.
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._in_flight: dict[str, set[str]] = {}
        self._published_timeouts: dict[str, float] = {}

        self._running = False
        self._tasks: list[asyncio.Task] = []
        # Pool-saturation back-off (ADR-084 §3.4). The delay doubles on each
        # consecutive saturation and resets on the next successful read, so a burst
        # degrades gracefully instead of spinning on the exhausted pool.
        self._saturation_backoff_seconds = _SATURATION_BACKOFF_SECONDS
        self._saturation_backoff_ceiling_seconds = _SATURATION_BACKOFF_CEILING_SECONDS
        # Per-tool ACL (ADR-066 §4.1). The value is the sentinel ``_UNSET`` when
        # the caller did not pass ``acl=`` (legacy, no constraint); the sentinel is
        # filtered out of ``acl_for`` so the legacy callers see ``None`` (the
        # pre-v0.16 contract). Otherwise the value is the ``ToolACL`` the caller
        # passed (or ``default_acl()`` for an explicit ``acl=None``).
        self._acls: dict[str, ToolACL | object] = {}
        # Observability surface: the consume loop updates the counters and
        # timestamps on every message; the heartbeat log line is emitted by
        # ``_consume_loop`` itself.
        self._messages_processed_total: int = 0
        self._messages_failed_total: int = 0
        self._last_activity_at: float = time.monotonic()
        self._last_heartbeat_at: float = 0.0
        self._last_error: str | None = None
        # Disabled when non-positive. The default mirrors the dispatcher's so an
        # operator looking at tail -f sees a liveness line from each component on
        # the same cadence.
        self._heartbeat_interval_seconds: float = heartbeat_interval_seconds

    def register(
        self,
        tool_cls: type,
        *,
        acl: ToolACL | None = _UNSET,  # type: ignore[assignment]
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        """Register a class decorated with @tool_worker.

        ``acl`` (ADR-066 §4.1, ADR-061 §5) is the per-tool authorisation
        consulted by ``_process_message`` before invoking the worker. The
        default (no ``acl=`` kwarg) preserves the pre-v0.16 behaviour: no
        constraint. Pass ``acl=default_acl()`` for the framework baseline
        (``PrincipalLevel.agent``, tenant-unpinned) or a stricter
        ``ToolACL(tenant_pinned=True, ...)`` for tenant-scoped tools.
        """
        if not hasattr(tool_cls, "name"):
            raise TypeError("Tool must be decorated with @tool_worker")
        # ADR-066 §4.4: the no-``acl=`` form is deprecated in v0.17; the legacy
        # path is ``register(tool_cls)`` with no policy attached (no constraint).
        # The warning is scoped to the ``acl is _UNSET`` branch (the caller did
        # not pass ``acl=`` at all). Passing ``acl=None`` is the explicit opt-out
        # and does NOT warn (the caller acknowledges the policy choice).
        if acl is _UNSET:
            import warnings

            warnings.warn(
                (
                    "WorkerManager.register(tool_cls) without an explicit "
                    "acl= kwarg is deprecated and will be removed in v0.18 "
                    "(ADR-066 §4.4). Pass acl=default_acl() for the "
                    "framework baseline or acl=ToolACL(...) for a stricter "
                    "policy. Pass acl=None to explicitly opt out (no "
                    "warning)."
                ),
                DeprecationWarning,
                stacklevel=2,
            )
        self._tools[tool_cls.name] = tool_cls
        declared_timeout = getattr(tool_cls, "__tool_worker_timeout_seconds__", None)
        if declared_timeout is not None:
            self._published_timeouts[tool_cls.name] = float(declared_timeout)
        # ``acl`` is the sentinel ``_UNSET`` when the caller omitted the kwarg
        # (legacy, no constraint). ``acl=None`` is the EXPLICIT opt-out (ADR-066 §4.4)
        # — also no constraint. Otherwise the value is the ``ToolACL``.
        if acl is _UNSET or acl is None:
            self._acls[tool_cls.name] = _UNSET
        else:
            self._acls[tool_cls.name] = acl

        # ADR-078 §5.5: a sync ``invoke`` MUST declare how it runs. The
        # ``__tool_worker_cpu_bound__`` marker no longer buys a fall-through —
        # ``is_cpu_bound`` classified every sync ``invoke`` as cpu-bound, so the
        # gate never fired for the tools it targeted and the 2026-10-07 OOM kept
        # its path.
        #
        # ``legacy_pool=True`` exempts the tool: the pool opened in ``start()``
        # would otherwise be unreachable for the very tools it exists to serve
        # (ADR-078 §12.4).
        if executor_factory is None and not self.__dict__.get("_legacy_pool", False):
            reject_undeclared_sync_tool(tool_cls)

        # ADR-069: optional per-tool dispatch override. The factory replaces the
        # internal ``ProcessPoolExecutor`` path with a custom async callable that
        # returns the standard ``result_dict`` shape. ``None`` keeps the legacy
        # path (internal pool, ``run_in_executor``). Lifecycle of the underlying
        # executor (process pool, thread pool, sidecar, GPU context, ...) is the
        # caller's responsibility; the Manager only invokes the factory once per
        # message.
        #
        # ``__dict__.setdefault`` keeps ``register()`` robust to ``__new__``-based
        # unit tests that skip ``__init__``.
        self.__dict__.setdefault("_executors", {})[tool_cls.name] = executor_factory

    def published_timeouts(self) -> Mapping[str, float]:
        """Return the timeouts declared by registered tools."""
        return dict(self._published_timeouts)

    def published_timeout_for(self, tool_name: str) -> float | None:
        """Return one tool's declared timeout, or ``None``."""
        return self._published_timeouts.get(tool_name)

    def acl_for(self, name: str) -> ToolACL | None:
        """Return a tool's ACL, or ``None`` when it has no policy."""
        stored = self._acls.get(name)
        if stored is _UNSET or stored is None:
            return None
        return stored  # type: ignore[return-value]

    def get(self, name: str) -> type | None:
        """Return a registered tool class, or ``None``."""
        return self._tools.get(name)

    def _stream_key(self, tool_name: str) -> str:
        """Return the namespaced Redis Stream key for a tool."""
        return tool_queue_key(self._key_prefix, tool_name)

    def names(self) -> list[str]:
        """Return the names of all registered tools."""
        return list(self._tools.keys())

    def list_descriptors(self) -> list[ToolDescriptor]:
        """Return serialisable descriptors for all registered tools."""
        out: list[ToolDescriptor] = []
        for name in self.names():
            tool_cls = self._tools[name]
            schema = getattr(tool_cls, "input_schema", None)
            schema_json = schema_to_json(schema)
            if schema_json is None:
                continue
            out.append(
                ToolDescriptor(
                    name=name,
                    description=getattr(tool_cls, "description", ""),
                    input_schema_json=schema_json,
                )
            )
        return out

    def _maybe_open_deprecated_pool(self) -> bool:
        """Open the deprecated internal pool when enabled."""
        return maybe_open_deprecated_pool(self)

    async def _reconnect_after_transport_error(self) -> None:
        """Reconnect Redis after a transport failure."""
        await reconnect_after_transport_error(self._redis)

    async def _back_off_on_saturation(self, tool_name: str) -> None:
        """Delay after Redis pool saturation."""
        await back_off_on_saturation(self, tool_name)

    async def _handle_consume_error(self, tool_name: str, error: Exception) -> None:
        """Recover from a consume-loop failure."""
        await handle_consume_error(self, tool_name, error)

    def _mark_in_flight(self, tool_name: str, message_id: str) -> None:
        """Record a running message."""
        mark_in_flight(self, tool_name, message_id)

    def _clear_in_flight(self, tool_name: str, message_id: str) -> None:
        """Forget a completed message."""
        clear_in_flight(self, tool_name, message_id)

    def _is_in_flight(self, tool_name: str, message_id: str) -> bool:
        """Return whether a message is running."""
        return is_in_flight(self, tool_name, message_id)

    async def _reaper_loop_once(self, tool_name: str) -> None:
        """Run one reaper pass."""
        await reaper_loop_once(self, tool_name)

    async def _pending_payload(self, stream_key: str, msg_id: str) -> dict:
        """Read a pending message payload."""
        return await pending_payload(self._redis, stream_key, msg_id)

    def _reclaim_threshold_ms(self, payload: dict, fallback_ms: int) -> int:
        """Return a message reclaim threshold."""
        return reclaim_threshold_ms(payload, fallback_ms)

    async def _parse_message_payload(
        self,
        *,
        tool_name: str,
        message_id: str,
        message_data: dict,
        stream_key: str,
    ) -> Event | None:
        """Decode a request payload."""
        return await parse_message_payload(
            self, tool_name, message_id, message_data, stream_key
        )

    def _evaluate_acl(
        self,
        *,
        tool_name: str,
        request_event: Event,
    ) -> str | None:
        """Evaluate the tool ACL."""
        return evaluate_tool_acl(self, tool_name, request_event)

    async def _emit_acl_denied(
        self,
        *,
        tool_name: str,
        stream_key: str,
        message_id: str,
        idempotency_key: str,
        request_event: Event,
        denied_reason: str,
    ) -> None:
        """Publish an ACL denial."""
        await emit_acl_denied(
            self,
            tool_name,
            stream_key,
            message_id,
            idempotency_key,
            request_event,
            denied_reason,
        )

    async def _translate_completion_to_event(
        self,
        *,
        tool_name: str,
        request_event: Event,
        idempotency_key: str,
        result: ToolResult,
    ) -> None:
        """Publish a tool result event."""
        await translate_completion_to_event(
            self, tool_name, request_event, idempotency_key, result
        )

    async def _dispatch_to_tool(
        self,
        *,
        tool_name: str,
        tool_cls: type,
        idempotency_key: str,
        tool_params: Mapping[str, JsonValue],
    ) -> ToolResult:
        """Dispatch a tool request."""
        return await dispatch_tool(
            self, tool_name, tool_cls, idempotency_key, tool_params
        )

    # The following lifecycle helpers form the manager's public surface.
    # They are kept together so callers can discover the supported operations
    # without reading the implementation modules. The comments intentionally
    # provide context for maintainers who are changing queue ownership.
    #
    # Redis streams represent pending work as a consumer-group PEL. The manager
    # therefore treats acknowledgement, recovery, and failure reporting as one
    # lifecycle concern even when their implementation details live elsewhere.
    #
    # Tool execution remains asynchronous. The consume loop schedules work under
    # a per-tool semaphore, while the reaper observes the same semaphore and
    # skips messages that are already running. This prevents duplicate side
    # effects without coupling the reaper to a particular execution factory.
    #
    # Timeouts are declared with the tool and recorded here at registration.
    # The reaper runs inside this manager, so it reads the declaration
    # directly and derives each request's deadline from it. Keeping the
    # declaration at registration avoids two competing timeout sources for
    # the same tool.
    #
    # ACL policy is selected at registration as well. An explicit policy is
    # evaluated before dispatch, and an omitted policy remains distinguishable
    # from an explicit opt-out for compatibility with existing callers.
    #
    # The remaining methods expose query and mutation operations used by the
    # HTTP layer, the memory subsystem, and the internal task loops. Their
    # contracts are intentionally narrow so the manager does not become a
    # second tool registry or event store.
    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def _compute_max_workers(self) -> int:
        """Delegate to ``compute_max_workers`` in
        ``_dispatch_helpers.py``. Kept as a method on the Manager so the caller
        can stay ``self._compute_max_workers()`` in ``start()`` (preserves the
        existing call site)."""
        return compute_max_workers(self._tools, explicit_cap=self._max_pool_workers)

    async def start(self) -> None:
        """Starts the worker manager."""
        if self._running:
            return

        self._running = True

        # ADR-078 §4.4: the internal pool is opt-in via ``legacy_pool=True``. It
        # is NOT inferred from ``max_pool_workers is None``: ``None`` is the
        # default and means "size the pool with the legacy formula", so gating on
        # it would silently disable the pool for every caller that never passed
        # the knob.
        self._deprecated_pool_opened = self._maybe_open_deprecated_pool()

        for tool_name in self._tools:
            # Ensure Consumer Group exists
            stream_key = self._stream_key(tool_name)
            try:
                await self._redis.xgroup_create(
                    stream_key, self._group_name, id="0", mkstream=True
                )
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    logger.exception(
                        "worker.xgroup_create.failed",
                        tool=tool_name,
                        stream_key=stream_key,
                        error=str(e),
                    )

            # Start consumer loop
            task = asyncio.create_task(self._consume_loop(tool_name))
            self._tasks.append(task)

            # Start reaper loop for this tool
            reaper_task = asyncio.create_task(self._reaper_loop(tool_name))
            self._tasks.append(reaper_task)

    async def stop(self) -> None:
        """Stops all consumers and shuts down the process pool."""
        self._running = False
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._pool:
            self._pool.shutdown(wait=True)
            self._pool = None

    async def _consume_loop(self, tool_name: str) -> None:
        stream_key = self._stream_key(tool_name)
        tool_cls = self._tools[tool_name]
        max_concurrency = getattr(tool_cls, "__tool_worker_max_concurrency__", 16)
        sem = asyncio.Semaphore(max_concurrency)
        # Published so the ``_reaper_loop`` acquires the same semaphore when it
        # reclaims PEL messages — without it the reaper bypasses the concurrency
        # limit and fires parallel tasks that multiply RAM use. post-mortem
        # 2026-10-07.
        self._semaphores[tool_name] = sem

        async def _process_with_sem(msg_id: str, msg_data: dict) -> None:
            async with sem:
                self._mark_in_flight(tool_name, msg_id)
                try:
                    await self._process_message(tool_name, stream_key, msg_id, msg_data)
                finally:
                    self._clear_in_flight(tool_name, msg_id)

        in_flight: set[asyncio.Task] = set()

        while self._running:
            try:
                # Block for 1 second waiting for new messages
                response = await self._redis.xreadgroup(
                    groupname=self._group_name,
                    consumername=self._consumer_name,
                    streams={stream_key: ">"},
                    count=max(1, min(max_concurrency, 32)),
                    block=1000,
                )
                # The pool served the request — an empty stream is still a
                # served one — so the saturation back-off goes back to its
                # floor (ADR-084 §3.4).
                self._saturation_backoff_seconds = _SATURATION_BACKOFF_SECONDS

                if not response:
                    # ``xreadgroup`` returned with no messages. In production the
                    # upstream ``block=1000`` makes this arm rare; under mocks (and
                    # any future non-blocking xreadgroup path) the loop would
                    # busy-spin, starving the sibling ``_reaper_loop`` of the event
                    # loop. A zero-second sleep is a yield-to-scheduler with no
                    # production cost.
                    await asyncio.sleep(0)
                    self._maybe_emit_heartbeat(tool_name)
                    continue

                for _, messages in response:
                    for message_id, message_data in messages:
                        task = asyncio.create_task(
                            _process_with_sem(message_id.decode(), message_data)
                        )
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)

                # Refresh liveness on every successful read; the heartbeat
                # distinguishes "loop idle because the stream is empty" from "loop
                # stuck because Redis stopped responding".
                self._last_activity_at = time.monotonic()
                self._maybe_emit_heartbeat(tool_name)

            except asyncio.CancelledError:
                break
            except Exception as e:
                # ``logger.exception`` routes the full traceback to the log handler.
                # Without it, an operator who sees the loop go silent cannot tell
                # whether the consumer is reconnecting to Redis, choking on a
                # payload parser, or stuck inside ``_process_message``.
                logger.exception(
                    "worker.consume_loop.error",
                    tool=tool_name,
                    error=str(e),
                )
                self._last_error = repr(e)
                # A saturated pool backs off inside the handler and stops there;
                # every other failure takes the standard one-second delay.
                await self._handle_consume_error(tool_name, e)

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    def _maybe_emit_heartbeat(self, tool_name: str) -> None:
        """Emit a structured liveness line on the configured cadence."""
        if self._heartbeat_interval_seconds <= 0:
            return
        now = time.monotonic()
        if now - self._last_heartbeat_at < self._heartbeat_interval_seconds:
            return
        self._last_heartbeat_at = now
        logger.info(
            "worker.consume_loop.heartbeat",
            tool=tool_name,
            messages_processed_total=self._messages_processed_total,
            messages_failed_total=self._messages_failed_total,
            idle_seconds=now - self._last_activity_at,
            last_error=self._last_error,
        )

    async def _process_message(
        self, tool_name: str, stream_key: str, message_id: str, message_data: dict
    ) -> None:
        tool_cls = self._tools[tool_name]
        retries_allowed = getattr(tool_cls, "__tool_worker_retries__", 3)

        request_event = await self._parse_message_payload(
            tool_name=tool_name,
            message_id=message_id,
            message_data=message_data,
            stream_key=stream_key,
        )
        if request_event is None:
            return  # payload parse already XACKed + counted
        tool_params = coerce_tool_params(request_event)
        idempotency_key = str(request_event.event_id)
        denied_reason = self._evaluate_acl(
            tool_name=tool_name,
            request_event=request_event,
        )
        if denied_reason is not None:
            await self._emit_acl_denied(
                tool_name=tool_name,
                stream_key=stream_key,
                message_id=message_id,
                idempotency_key=idempotency_key,
                request_event=request_event,
                denied_reason=denied_reason,
            )
            return

        try:
            result = await self._dispatch_to_tool(
                tool_name=tool_name,
                tool_cls=tool_cls,
                idempotency_key=idempotency_key,
                tool_params=tool_params,
            )

            await self._translate_completion_to_event(
                tool_name=tool_name,
                request_event=request_event,
                idempotency_key=idempotency_key,
                result=result,
            )

            # Acknowledge the message since it was processed (success or
            # explicit failure)
            await self._redis.xack(stream_key, self._group_name, message_id)

        except Exception as e:
            # A hard crash (e.g. process died, OOM, exception in invoke
            # outside Result)
            logger.exception(
                "worker.tool.hard_crash",
                tool=tool_name,
                message_id=message_id,
                error=str(e),
            )
            self._messages_failed_total += 1
            self._last_error = repr(e)

            # If the process pool itself broke, we can't do much but we must not
            # XACK. We let the Reaper pick it up via XAUTOCLAIM. But we can check
            # the delivery count via XPENDING to see if it exceeded retries.
            escalated = await escalate_on_retry_budget_exceeded(
                redis=self._redis,
                stream_key=stream_key,
                group_name=self._group_name,
                message_id=message_id,
                retries_allowed=retries_allowed,
                tool_name=tool_name,
                request_event=request_event,
                idempotency_key=idempotency_key,
                reason="worker_crash",
                event_log=self._event_log,
            )
            if escalated:
                # Framework escalation emitted ``.failed(permanent)``;
                # ACK so the consumer group stops holding the message.
                await self._redis.xack(stream_key, self._group_name, message_id)

    async def _reaper_loop(self, tool_name: str) -> None:
        """Periodically recover pending messages for one tool."""
        while self._running:
            try:
                await asyncio.sleep(self._reaper_interval)
                await self._reaper_loop_once(tool_name)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(
                    "worker.reaper.error",
                    tool=tool_name,
                    error=str(e),
                )
                self._last_error = repr(e)
