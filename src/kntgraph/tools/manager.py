# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Worker Manager - orchestrates the Tool Worker Pattern (ADR-036).

The ``WorkerManager`` is the single tool execution path
(ADR-066 §3.1: ``ToolRegistry`` is removed). Each
``register(tool_cls, *, acl=None)`` call optionally
attaches a ``ToolACL`` that ``_process_message``
consults before invoking the worker (gate-1 of the
three-gate ACL model, ADR-060 §3.0). On ACL denial
the worker emits ``tool.<name>.failed`` with reason
``acl_denied`` and acks the message — the request
fails fast without consuming a worker slot.

The ``acl_for(name)`` accessor mirrors the
``ToolRegistry.acl_for`` surface so the existing
``WorkerManager`` callers (test fixtures, CLI
scaffolds) can switch with a one-line rename. The
canonical ACL home stays ``kntgraph.tools.acl``;
``WorkerManager`` re-exports ``ToolACL`` and
``default_acl`` for discoverability.

ACL semantics
-------------

  - ``register(tool_cls)`` (legacy, no ``acl=``):
    **no constraint** — the tool is invoked for
    every request. This preserves backward
    compatibility for callers that registered
    tools before v0.16. The migration path is to
    re-register with ``acl=default_acl()`` (the
    framework baseline: ``Role.agent``, tenant
    unpinned) or a stricter ``ToolACL``.
  - ``register(tool_cls, acl=default_acl())``
    (explicit baseline): every request is checked
    against ``Role.agent``. The worker refuses the
    request if the principal's role is below
    ``agent`` or the tenant does not match.
  - ``register(tool_cls, acl=ToolACL(...))``
    (custom): the caller's custom policy. The
    worker refuses the request if ``acl.check(p)``
    returns ``False``.

The ``producer_principal_id`` stamped on the event
at the request boundary (per ADR-066 §4.1) is the
input to ``acl.check``. Events that predate v0.16
(``producer_principal_id=None``) are denied when
``acl`` is set — the audit trail records
``acl_denied_no_principal``.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Optional, Type

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.context import BaseContext

    from kntgraph.infra.redis import RedisLike

import structlog

from kntgraph.core.event import Event
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools._worker_invocation import _invoke_tool_sync
from kntgraph.tools.acl import ToolACL, default_acl

logger = structlog.get_logger()

# Sentinel used to differentiate "the caller did not
# pass ``acl=``" (legacy, no constraint) from "the
# caller passed ``acl=None`` explicitly" (the new
# default-allow-with-no-policy contract). The legacy
# path is preserved at the API level: omitting
# ``acl=`` keeps the pre-v0.16 behaviour. Passing
# ``acl=None`` is reserved for future use (e.g. the
# v0.17 step flips the default to ``default_acl()``
# and uses this sentinel to detect the explicit opt
# out).
_UNSET: "object" = object()

# ``_invoke_tool_sync`` is re-exported here for the test
# suite (which historically monkey-patched it on
# ``kntgraph.tools.manager``) and for any external code
# that relied on the symbol. The canonical definition
# lives in ``_worker_invocation`` so the ``spawn`` start
# method can pickle the callable by reference without
# pulling the rest of the package into the worker.
__all__ = [
    "ToolACL",
    "WorkerManager",
    "_invoke_tool_sync",
    "default_acl",
]


_SPAWN_METHOD = "spawn"


class WorkerManager:
    """
    Manages the lifecycle of Tool Workers.
    Listens to Redis Streams (via Consumer Groups) and delegates execution
    to a ProcessPoolExecutor to avoid blocking the main event loop.
    """

    def __init__(
        self,
        redis: "RedisLike",
        event_log: EventLog,
        group_name: str = "fmh_tool_workers",
        consumer_name: str = "worker-1",
        reaper_interval: float = 60.0,
        reaper_idle_time: float = 300.0,
        heartbeat_interval_seconds: float = 30.0,
    ):
        self._redis = redis
        self._event_log = event_log
        self._group_name = group_name
        self._consumer_name = consumer_name

        self._reaper_interval = reaper_interval
        self._reaper_idle_time = reaper_idle_time
        self._tools: dict[str, Type] = {}
        self._pool: ProcessPoolExecutor | None = None
        # Cached in ``start()``; stored here so tests can
        # assert on it without re-deriving the default.
        self._mp_context: "BaseContext | None" = None

        self._running = False
        self._tasks: list[asyncio.Task] = []
        # Per-tool ACL (ADR-066 §4.1). The value is
        # the sentinel ``_UNSET`` when the caller did
        # not pass ``acl=`` (legacy, no constraint);
        # the sentinel ``_UNSET`` is filtered out of
        # ``acl_for`` so the legacy callers see
        # ``None`` (the pre-v0.16 contract). When the
        # caller passes ``acl=...``, the value is the
        # ``ToolACL`` they passed (or ``default_acl()``
        # for ``acl=None`` explicitly).
        self._acls: dict[str, "ToolACL | object"] = {}
        # Observability surface: the consume loop updates the
        # counters and timestamps on every message; the heartbeat
        # log line is emitted by ``_consume_loop`` itself.
        self._messages_processed_total: int = 0
        self._messages_failed_total: int = 0
        self._last_activity_at: float = time.monotonic()
        self._last_heartbeat_at: float = 0.0
        self._last_error: str | None = None
        # Disabled when non-positive. The default mirrors the
        # dispatcher's so an operator looking at tail -f sees a
        # liveness line from each component on the same cadence.
        self._heartbeat_interval_seconds: float = heartbeat_interval_seconds

    def register(
        self,
        tool_cls: Type,
        *,
        acl: Optional[ToolACL] = _UNSET,  # type: ignore[assignment]
    ) -> None:
        """Register a class decorated with @tool_worker.

        ``acl`` (ADR-066 §4.1, ADR-061 §5) is the
        per-tool authorisation consulted by
        ``_process_message`` before invoking the
        worker. The default (no ``acl=`` kwarg)
        preserves the pre-v0.16 behaviour: no
        constraint. Pass ``acl=default_acl()`` for
        the framework baseline (``Role.agent``,
        tenant-unpinned) or a stricter
        ``ToolACL(tenant_pinned=True, ...)`` for
        tenant-scoped tools.
        """
        if not hasattr(tool_cls, "name"):
            raise TypeError("Tool must be decorated with @tool_worker")
        self._tools[tool_cls.name] = tool_cls
        # ``acl`` is the sentinel ``_UNSET`` when the
        # caller omitted the kwarg (legacy, no
        # constraint). Otherwise the value is the
        # ``ToolACL`` (or ``default_acl()`` if the
        # caller explicitly passed ``acl=None``).
        if acl is _UNSET:
            self._acls[tool_cls.name] = _UNSET
        else:
            self._acls[tool_cls.name] = (
                acl if acl is not None else default_acl()
            )

    def acl_for(self, name: str) -> Optional[ToolACL]:
        """Return the ``ToolACL`` for ``name`` (or
        ``None`` if the tool is not registered, or
        was registered without an explicit
        ``acl=``). The framework reads this at
        invoke time so the gate-1 ACL check does not
        need a separate lookup.

        The surface mirrors ``ToolRegistry.acl_for``
        (removed in v0.18 per ADR-066 §4.4); the
        rename is a one-line ``r.acl_for(n)``
        → ``wm.acl_for(n)``.
        """
        stored = self._acls.get(name)
        if stored is _UNSET or stored is None:
            return None
        return stored  # type: ignore[return-value]

    async def start(self) -> None:
        """Starts the worker manager."""
        if self._running:
            return

        self._running = True

        # Calculate max workers across all registered tools, minimum 2
        max_workers = sum(
            getattr(t, "__tool_worker_max_concurrency__", 1)
            for t in self._tools.values()
        )
        max_workers = max(2, max_workers)

        # Always use ``spawn`` — container runtimes (and
        # any process that has imported ``threading`` +
        # ``ssl`` + ``cryptography`` + ``redis.asyncio``
        # + ``pydantic`` + ``litellm`` before this point)
        # corrupt the forked child's ``threading._RLock``
        # / ``select`` state under the default ``fork``
        # start method and stall the Redis consumer loop
        # (``xreadgroup`` never returns). ``spawn`` starts
        # a fresh interpreter per worker; the cost is a
        # ~50-200ms import overhead per cold worker, the
        # gain is a deadlock-free execution path. See
        # ADRs/ADR-054-WorkerManager-Transport-Evaluation.md
        # lines 269-273 for the prior art.
        self._mp_context = multiprocessing.get_context(_SPAWN_METHOD)
        self._pool = ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=self._mp_context,
        )

        for tool_name in self._tools:
            # Ensure Consumer Group exists
            stream_key = f"knt:tools:{tool_name}:queue"
            try:
                await self._redis.xgroup_create(
                    stream_key, self._group_name, id="0", mkstream=True
                )
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    logger.error(
                        "worker.xgroup_create.failed",
                        tool=tool_name,
                        stream_key=stream_key,
                        error=str(e),
                        exc_info=True,
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
        stream_key = f"knt:tools:{tool_name}:queue"
        while self._running:
            try:
                # Block for 1 second waiting for new messages
                response = await self._redis.xreadgroup(
                    groupname=self._group_name,
                    consumername=self._consumer_name,
                    streams={stream_key: ">"},
                    count=1,
                    block=1000,
                )

                if not response:
                    # ``xreadgroup`` returned with no messages. In
                    # production the upstream ``block=1000`` makes this
                    # arm rare; under mocks (and any future
                    # non-blocking xreadgroup path) the loop would
                    # busy-spin, starving the sibling ``_reaper_loop``
                    # of the event loop. A zero-second sleep is a
                    # yield-to-scheduler with no production cost.
                    await asyncio.sleep(0)
                    self._maybe_emit_heartbeat(tool_name)
                    continue

                for _, messages in response:
                    for message_id, message_data in messages:
                        await self._process_message(
                            tool_name, stream_key, message_id.decode(), message_data
                        )
                # Refresh liveness on every successful read; the
                # heartbeat distinguishes "loop idle because the
                # stream is empty" from "loop stuck because Redis
                # stopped responding".
                self._last_activity_at = time.monotonic()
                self._maybe_emit_heartbeat(tool_name)

            except asyncio.CancelledError:
                break
            except Exception as e:
                # ``exc_info=True`` routes the full traceback to the
                # log handler. Without it, an operator who sees the
                # loop go silent cannot tell whether the consumer
                # is reconnecting to Redis, choking on a payload
                # parser, or stuck inside ``_process_message``.
                logger.error(
                    "worker.consume_loop.error",
                    tool=tool_name,
                    error=str(e),
                    exc_info=True,
                )
                self._last_error = repr(e)
                await asyncio.sleep(1)
                self._maybe_emit_heartbeat(tool_name)

    def _maybe_emit_heartbeat(self, tool_name: str) -> None:
        """Emit a structured liveness line on the cadence
        ``_heartbeat_interval_seconds``. Disabled when the
        interval is non-positive. The line carries the message
        counters, the time since the last successful read, and
        the last error string (if any).
        """
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

        try:
            payload_str = message_data.get(b"payload", b"{}").decode()
            request_event_dict = json.loads(payload_str)
            request_event = Event.from_dict(request_event_dict)
        except Exception as e:
            logger.error(
                "worker.payload_parse.error",
                tool=tool_name,
                message_id=message_id,
                error=str(e),
                exc_info=True,
            )
            await self._redis.xack(stream_key, self._group_name, message_id)
            self._messages_failed_total += 1
            return

        tool_params = (
            request_event.data.get("params") or request_event.data.get("args") or {}
        )
        idempotency_key = str(request_event.event_id)

        # Gate-1 ACL check (ADR-060 §3.0, ADR-061 §5,
        # ADR-066 §4.1). The worker refuses the request
        # before consuming a worker slot — the canonical
        # "deny-by-construction" pattern. We use the
        # ``producer_principal_id`` stamped on the event
        # at the request boundary (the API layer in v0.16
        # sets it from ``principal_ctx``; older events
        # pass ``None`` and we deny with a clear
        # ``reason`` so the operator can diagnose).
        acl = self.acl_for(tool_name)
        principal_id = request_event.producer_principal_id
        denied_reason: Optional[str] = None
        if acl is None:
            # Tool registered without ``acl=`` (legacy).
            # Default-allow: the operator must opt in to
            # the new ACL surface by re-registering with
            # ``acl=default_acl()``. This matches the
            # ADR-066 migration path: the v0.16 step
            # ships the hook + default ACL; the v0.17
            # step flips the default to deny (so the
            # gap closes by construction in production).
            pass
        elif principal_id is None:
            # Event predates v0.16 — no principal
            # stamped. We deny with
            # ``acl_denied_no_principal`` so the audit
            # trail records why.
            denied_reason = "acl_denied_no_principal"
        else:
            from kntgraph.security import Principal, Role

            # ``producer_principal_id`` is the principal's
            # ``agent_id`` (the format the API layer
            # produces from ``principal_ctx``: per the
            # same convention as ``Principal.agent_id``,
            # it starts with the tenant, e.g.
            # ``tenant-a.agent-1``). We extract the
            # tenant prefix for the ``Principal``
            # invariant (non-admin requires a
            # non-empty tenant). If the format is
            # ambiguous, fall back to the whole string
            # as the tenant (single-segment legacy).
            tenant_id = principal_id.partition(".")[0] or principal_id
            principal = Principal(
                agent_id=principal_id,
                role=Role.agent,
                tenant_id=tenant_id,
                key_id="worker",
            )
            ok, reason = acl.check(principal)
            if not ok:
                denied_reason = f"acl_denied:{reason}"

        if denied_reason is not None:
            logger.warning(
                "worker.acl_denied",
                tool=tool_name,
                message_id=message_id,
                reason=denied_reason,
                producer_principal_id=principal_id,
            )
            denied_evt = Event.create(
                event_type=f"tool.{tool_name}.failed",
                agent_id=request_event.agent_id,
                event_class="domain",
                causation_id=uuid.UUID(idempotency_key),
                data={
                    "error": denied_reason,
                    "request_id": idempotency_key,
                },
                correlation=request_event.correlation,
                producer_principal_id=principal_id,
            )
            await self._event_log.append(denied_evt)
            await self._redis.xack(stream_key, self._group_name, message_id)
            self._messages_failed_total += 1
            return

        try:
            # We use asyncio.get_running_loop().run_in_executor to run the tool synchronously
            # in a separate process. The wrapper _invoke_tool_sync will handle the asyncio loop inside the process.
            loop = asyncio.get_running_loop()
            # ``run_in_executor`` is typed strictly (``args: _Ts``);
            # we wrap the call in ``cast(Any, (...))`` so the
            # executor accepts the heterogeneous tuple
            # ``(Type[X], str, dict[str, JsonValue])``. The
            # wrapper signature is enforced at runtime by
            # ``_invoke_tool_sync``.
            result_dict = await loop.run_in_executor(
                self._pool,
                _invoke_tool_sync,  # type: ignore[arg-type]
                tool_cls,
                idempotency_key,
                tool_params,
            )

            # Translate to Domain Events. ADR-037: pass
            # ``correlation=request_event.correlation`` so
            # the completion keeps the same flow id as
            # the request. The WorkerManager runs in its
            # own asyncio task (ContextVar is empty), so
            # it MUST thread the correlation through the
            # event object directly.
            if result_dict["status"] == "ok":
                completed_evt = Event.create(
                    event_type=f"tool.{tool_name}.completed",
                    agent_id=request_event.agent_id,
                    event_class="domain",
                    causation_id=uuid.UUID(idempotency_key),
                    data=result_dict["value"],
                    correlation=request_event.correlation,
                )
                await self._event_log.append(completed_evt)
                self._messages_processed_total += 1
            else:
                failed_evt = Event.create(
                    event_type=f"tool.{tool_name}.failed",
                    agent_id=request_event.agent_id,
                    event_class="domain",
                    causation_id=uuid.UUID(idempotency_key),
                    data={"error": result_dict["error"]},
                    correlation=request_event.correlation,
                )
                await self._event_log.append(failed_evt)
                self._messages_failed_total += 1

            # Acknowledge the message since it was processed (success or explicit failure)
            await self._redis.xack(stream_key, self._group_name, message_id)

        except Exception as e:
            # A hard crash (e.g. process died, OOM, exception in invoke outside Result)
            logger.error(
                "worker.tool.hard_crash",
                tool=tool_name,
                message_id=message_id,
                error=str(e),
                exc_info=True,
            )
            self._messages_failed_total += 1
            self._last_error = repr(e)

            # If the process pool itself broke, we can't do much but we must not XACK.
            # We let the Reaper pick it up via XAUTOCLAIM.
            # But we can proactively check delivery count via XPENDING to see if it exceeded retries.
            pending_info = await self._redis.xpending_range(
                stream_key, self._group_name, min=message_id, max=message_id, count=1
            )
            if pending_info:
                delivery_count = pending_info[0]["times_delivered"]
                if delivery_count > retries_allowed:
                    # DLQ trigger!
                    logger.error(
                        "worker.dlq.triggered",
                        tool=tool_name,
                        message_id=message_id,
                        delivery_count=delivery_count,
                        retries_allowed=retries_allowed,
                    )
                    failed_evt = Event.create(
                        event_type=f"tool.{tool_name}.failed",
                        agent_id=request_event.agent_id,
                        event_class="domain",
                        causation_id=uuid.UUID(idempotency_key),
                        data={
                            "error": f"Max retries exceeded / Worker crash: {str(e)}"
                        },
                        correlation=request_event.correlation,
                    )
                    await self._event_log.append(failed_evt)
                    await self._redis.xack(stream_key, self._group_name, message_id)
                    # We could also write to a DLQ stream here if needed.

    async def _reaper_loop(self, tool_name: str) -> None:
        """Periodically scans PEL and re-claims stuck messages (auto-recovery)."""
        stream_key = f"knt:tools:{tool_name}:queue"
        # Idle time is in milliseconds for redis
        idle_time_ms = int(self._reaper_idle_time * 1000)

        while self._running:
            try:
                await asyncio.sleep(self._reaper_interval)

                # claim messages pending for more than idle_time_ms
                # 0-0 means start from beginning
                claimed = await self._redis.xautoclaim(
                    name=stream_key,
                    groupname=self._group_name,
                    consumername=self._consumer_name,
                    min_idle_time=idle_time_ms,
                    start_id="0-0",
                    count=10,
                )

                # claimed[1] contains the actual messages we claimed
                messages = claimed[1]
                for message_id, message_data in messages:
                    # By claiming, we become the owner. The delivery_count incremented.
                    # We process it immediately.
                    logger.warning(
                        "worker.reaper.reclaimed",
                        tool=tool_name,
                        message_id=message_id.decode(),
                    )
                    # Process message concurrently so reaper isn't blocked
                    asyncio.create_task(
                        self._process_message(
                            tool_name, stream_key, message_id.decode(), message_data
                        )
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "worker.reaper.error",
                    tool=tool_name,
                    error=str(e),
                    exc_info=True,
                )
                self._last_error = repr(e)
