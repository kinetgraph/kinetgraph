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
     framework baseline: ``PrincipalLevel.agent``,
     tenant unpinned) or a stricter ``ToolACL``.
   - ``register(tool_cls, acl=default_acl())``
     (explicit baseline): every request is checked
     against ``PrincipalLevel.agent``. The worker refuses the
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
import inspect
import json
import multiprocessing
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from multiprocessing.context import BaseContext

    from kntgraph.infra.redis import RedisLike

from collections.abc import Awaitable

import structlog
from redis import exceptions as redis_exceptions

from kntgraph.core._typing import JsonValue
from kntgraph.core.event import Event
from kntgraph.infra.redis._prefix import validate_prefix
from kntgraph.infra.redis._tools import tool_queue_key
from kntgraph.stream.event_log.store import EventLog
from kntgraph.tools._worker_invocation import _invoke_tool_sync
from kntgraph.tools.acl import ToolACL, default_acl
from kntgraph.tools.descriptors import ToolDescriptor, schema_to_json

_ORIGINAL_INVOKE_TOOL_SYNC = _invoke_tool_sync

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
_UNSET: object = object()


# ADR-069: pluggable executor factory. A tool registered with
# ``executor_factory=`` (see ``WorkerManager.register``) gets
# this callable invoked once per message in place of the
# internal ``ProcessPoolExecutor``. The factory receives the
# sync ``_invoke_tool_sync`` (or compatible), the message's
# idempotency key (which is the event_id), and the parsed
# tool params; it returns a coroutine that resolves with
# the standard ``result_dict`` (status "ok" or "err"). The
# Manager remains the owner of consumer loop / XACK / reaper
# / ACL / observability; the factory is a black-box dispatch
# hook that lets callers route heavy tools (ML models,
# sidecars, GPU pins) to dedicated pools without forking
# the framework.
ExecutorFactory = Callable[
    [Callable[[type, str, dict[str, Any]], dict[str, Any]], str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]

# ``_invoke_tool_sync`` is re-exported here for the test
# suite (which historically monkey-patched it on
# ``kntgraph.tools.manager``) and for any external code
# that relied on the symbol. The canonical definition
# lives in ``_worker_invocation`` so the ``spawn`` start
# method can pickle the callable by reference without
# pulling the rest of the package into the worker.
__all__ = [
    "ToolACL",
    "ToolDescriptor",
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
    ):
        # ADR-076 / DEBT §2.35: namespace prefix for
        # every Redis key the manager writes or reads
        # (``knt:tools:<tool>:queue`` -- the consumer
        # group creation, the consume loop, and the
        # reaper loop all flow through ``_stream_key``
        # so the prefix is applied consistently).
        # Empty string (default) is byte-for-byte
        # identical to the pre-076 wire format.
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
        # Cached in ``start()``; stored here so tests can
        # assert on it without re-deriving the default.
        self._mp_context: BaseContext | None = None

        # Per-tool executor factory (ADR-069). When ``None`` for a
        # given tool, the Manager falls back to its internal
        # ``ProcessPoolExecutor``. When set, the factory is called
        # once per message in place of ``run_in_executor`` and
        # returns the standard ``result_dict`` shape. The Manager
        # remains the owner of XACK / events / reaper / ACL /
        # observability; the factory is a black-box dispatch hook.
        self._executors: dict[str, Any] = {}
        # ``max_pool_workers`` is the explicit cap on the internal
        # ``ProcessPoolExecutor``. ``None`` (default) means "use
        # the legacy formula ``max(1, sum(max_concurrency))``"; an
        # integer means "use exactly N workers regardless of the
        # number of tools". In Fargate-class environments with
        # memory caps, this is the lever that prevents the OOM
        # described in the post-mortem 2026-10-07.
        self._max_pool_workers: int | None = max_pool_workers

        # Per-tool ``asyncio.Semaphore`` (``max_concurrency``) — populated
        # em ``_consume_loop`` e consumido pelo ``_reaper_loop`` para
        # honrar o limite de tasks concorrentes por tool. Sem isso, o
        # reaper bypassa o semáforo e dispara N processamentos em
        # paralelo (um por mensagem no PEL) → OOM em ambiente com
        # workers carregando modelos ML pesados. post-mortem 2026-10-07.
        self._semaphores: dict[str, asyncio.Semaphore] = {}

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
        self._acls: dict[str, ToolACL | object] = {}
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
        tool_cls: type,
        *,
        acl: ToolACL | None = _UNSET,  # type: ignore[assignment]
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        """Register a class decorated with @tool_worker.

        ``acl`` (ADR-066 §4.1, ADR-061 §5) is the
        per-tool authorisation consulted by
        ``_process_message`` before invoking the
        worker. The default (no ``acl=`` kwarg)
        preserves the pre-v0.16 behaviour: no
        constraint. Pass ``acl=default_acl()`` for
        the framework baseline (``PrincipalLevel.agent``,
        tenant-unpinned) or a stricter
        ``ToolACL(tenant_pinned=True, ...)`` for
        tenant-scoped tools.
        """
        if not hasattr(tool_cls, "name"):
            raise TypeError("Tool must be decorated with @tool_worker")
        # ADR-066 §4.4: the no-``acl=`` form is
        # deprecated in v0.17; the legacy path is
        # ``register(tool_cls)`` with no policy
        # attached (no constraint). The warning is
        # scoped to the ``acl is _UNSET`` branch
        # (caller did not pass ``acl=`` at all).
        # Passing ``acl=None`` is the explicit
        # opt-out and does NOT warn (the caller is
        # acknowledging the policy choice).
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
        # ``acl`` is the sentinel ``_UNSET`` when the
        # caller omitted the kwarg (legacy, no
        # constraint). ``acl=None`` is the EXPLICIT
        # opt-out (ADR-066 §4.4) — also no constraint.
        # Otherwise the value is the ``ToolACL``.
        if acl is _UNSET or acl is None:
            self._acls[tool_cls.name] = _UNSET
        else:
            self._acls[tool_cls.name] = acl

        # ADR-069: optional per-tool dispatch override. The
        # factory replaces the internal ``ProcessPoolExecutor``
        # path with a custom async callable that returns the
        # standard ``result_dict`` shape. ``None`` keeps the
        # legacy path (internal pool, ``run_in_executor``).
        # Lifecycle of the underlying executor (process pool,
        # thread pool, sidecar, GPU context, ...) is the
        # caller's responsibility; the Manager only invokes
        # the factory once per message.
        #
        # ``__dict__.setdefault`` keeps ``register()`` robust to
        # ``__new__``-based unit tests that skip ``__init__`` and
        # only set the attrs they need — adding a new private
        # attr in the future does not require updating those
        # tests to seed a third dict.
        self.__dict__.setdefault("_executors", {})[tool_cls.name] = executor_factory

    def acl_for(self, name: str) -> ToolACL | None:
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

    def get(self, name: str) -> type | None:
        """Return the registered ``@tool_worker`` class
        for ``name`` (or ``None`` if not registered).

        Replaces ``ToolRegistry.get(name)`` (removed in
        v0.18 per ADR-066 §4.4). The returned class
        carries the ``name`` / ``description`` /
        ``input_schema`` attributes injected by the
        ``@tool_worker`` decorator, so callers that
        previously read ``registry.get(n).input_schema``
        can read ``wm.get(n).input_schema`` unchanged.
        """
        return self._tools.get(name)

    def _stream_key(self, tool_name: str) -> str:
        """Compose the namespaced Stream key for ``tool_name``.

        ADR-076 / DEBT §2.35: the consumer-group creation
        in ``start``, the consume loop, and the reaper
        loop all flow through this helper so the namespace
        prefix applies uniformly. Empty prefix returns the
        unprefixed ``knt:tools:<tool>:queue`` key (the
        pre-076 wire format).
        """
        return tool_queue_key(self._key_prefix, tool_name)

    def names(self) -> list[str]:
        """Return the names of every registered tool.

        Replaces ``ToolRegistry.names()`` (removed in
        v0.18 per ADR-066 §4.4).
        """
        return list(self._tools.keys())

    def list_descriptors(self) -> list[ToolDescriptor]:
        """Return a :class:`ToolDescriptor` for every
        registered ``@tool_worker``.

        Replaces ``ToolRegistry.list_descriptors()``
        (removed in v0.18 per ADR-066 §4.4). Used by
        the HTTP ``GET /agents/{id}/tools`` endpoint
        and by the
        :class:`kntgraph.agents.memory.solutions.SolutionPromoter`
        to populate ``(:Tool)`` nodes in the Solution
        sub-graph of FalkorDB.

        The serialisation logic lives in
        :func:`kntgraph.tools.descriptors.schema_to_json`
        (moved from ``registry.py`` in the same
        release). A tool whose schema is not
        serialisable / not round-trippable is skipped
        (the operator sees a ``warning`` log line).
        """
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

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    async def start(self) -> None:
        """Starts the worker manager."""
        if self._running:
            return

        self._running = True

        # Calculate max workers across all registered tools. The
        # explicit ``max_pool_workers`` cap (set in ``__init__``)
        # takes priority — useful in memory-capped environments
        # (Fargate 4 GB) where multiple concurrent workers would
        # load heavy ML models in parallel and OOM. The default
        # formula ``max(1, min(32, sum(max_concurrency)))`` keeps
        # one worker per tool at most, which is what most callers
        # want. The post-mortem 2026-10-07 scenario in the
        # backoffice is what motivated the ``max(1, ...)`` floor
        # (was ``max(2, ...)``).
        if self._max_pool_workers is not None:
            max_workers = self._max_pool_workers
        else:
            max_workers = sum(
                getattr(t, "__tool_worker_max_concurrency__", 1)
                for t in self._tools.values()
            )
            max_workers = max(1, min(32, max_workers))

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
        # Publica para o ``_reaper_loop`` adquirir o mesmo semáforo quando
        # reclaimer mensagens do PEL — sem isso, o reaper bypassa o
        # limite de concorrência e dispara tasks em paralelo que
        # multiplicam o uso de RAM (uma por worker do pool carregando
        # modelos ML). post-mortem 2026-10-07.
        self._semaphores[tool_name] = sem
        read_batch_size = max(1, min(max_concurrency, 32))

        async def _process_with_sem(msg_id: str, msg_data: dict) -> None:
            async with sem:
                await self._process_message(tool_name, stream_key, msg_id, msg_data)

        in_flight: set[asyncio.Task] = set()

        while self._running:
            try:
                # Block for 1 second waiting for new messages
                response = await self._redis.xreadgroup(
                    groupname=self._group_name,
                    consumername=self._consumer_name,
                    streams={stream_key: ">"},
                    count=read_batch_size,
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
                        task = asyncio.create_task(
                            _process_with_sem(message_id.decode(), message_data)
                        )
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)

                # Refresh liveness on every successful read; the
                # heartbeat distinguishes "loop idle because the
                # stream is empty" from "loop stuck because Redis
                # stopped responding".
                self._last_activity_at = time.monotonic()
                self._maybe_emit_heartbeat(tool_name)

            except asyncio.CancelledError:
                break
            except Exception as e:
                # ``logger.exception`` routes the full traceback to
                # the log handler. Without it, an operator who sees
                # the loop go silent cannot tell whether the consumer
                # is reconnecting to Redis, choking on a payload
                # parser, or stuck inside ``_process_message``.
                logger.exception(
                    "worker.consume_loop.error",
                    tool=tool_name,
                    error=str(e),
                )
                self._last_error = repr(e)
                if isinstance(e, RuntimeError):
                    close_fn = getattr(self._redis, "aclose", None) or getattr(
                        self._redis, "close", None
                    )
                    if close_fn:
                        try:
                            res = close_fn()
                            if asyncio.iscoroutine(res):
                                await res
                        except (
                            redis_exceptions.RedisError,
                            AttributeError,
                            OSError,
                            TimeoutError,
                        ):
                            pass
                    pool = getattr(self._redis, "connection_pool", None)
                    if pool and hasattr(pool, "disconnect"):
                        try:
                            dis_res = pool.disconnect()
                            if asyncio.iscoroutine(dis_res):
                                await dis_res
                        except (
                            redis_exceptions.RedisError,
                            AttributeError,
                            OSError,
                            TimeoutError,
                        ):
                            pass
                await asyncio.sleep(1)
                self._maybe_emit_heartbeat(tool_name)

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

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
            logger.exception(
                "worker.payload_parse.error",
                tool=tool_name,
                message_id=message_id,
                error=str(e),
            )
            await self._redis.xack(stream_key, self._group_name, message_id)
            self._messages_failed_total += 1
            return

        raw_params = (
            request_event.data.get("params") or request_event.data.get("args") or {}
        )
        tool_params = cast(
            dict[str, Any], raw_params if isinstance(raw_params, dict) else {}
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
        denied_reason: str | None = None
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
            from kntgraph.security import Principal, PrincipalLevel

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
                level=PrincipalLevel.agent,
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
            tool_instance = tool_cls()
            invoke_fn = getattr(tool_instance, "invoke", None)
            is_coro = inspect.iscoroutinefunction(invoke_fn)
            is_cpu = (
                getattr(tool_cls, "__tool_worker_cpu_bound__", False)
                or not is_coro
                or (_invoke_tool_sync is not _ORIGINAL_INVOKE_TOOL_SYNC)
            )

            # ADR-069: a per-tool ``executor_factory`` overrides the
            # internal ``ProcessPoolExecutor`` path. The Manager
            # still owns the surrounding concerns (XACK, events,
            # observability, ACL) — the factory is a black-box
            # dispatch hook invoked once per message. ``None`` keeps
            # the legacy CPU/async dispatch below; any callable
            # (sync wrapping a thread pool, a sidecar proxy, a
            # custom process pool, etc.) replaces it.
            executor_factory = self._executors.get(tool_name)
            if executor_factory is not None:
                result_dict = await executor_factory(
                    _invoke_tool_sync, idempotency_key, tool_params
                )
            elif is_cpu:
                loop = asyncio.get_running_loop()
                result_dict = await loop.run_in_executor(
                    self._pool,
                    _invoke_tool_sync,
                    tool_cls,
                    idempotency_key,
                    tool_params,
                )
            else:
                result = await tool_instance.invoke(
                    idempotency_key=idempotency_key, **tool_params
                )
                if result.is_ok():
                    result_dict = {"status": "ok", "value": result.unwrap()}
                else:
                    result_dict = {
                        "status": "err",
                        "error": str(result.err_value_or_raise()),
                    }

            # Translate to Domain Events. ADR-037: pass
            # ``correlation=request_event.correlation`` so
            # the completion keeps the same flow id as
            # the request. The WorkerManager runs in its
            # own asyncio task (ContextVar is empty), so
            # it MUST thread the correlation through the
            # event object directly.
            if result_dict["status"] == "ok":
                val = result_dict["value"]
                evt_data: dict[str, JsonValue] = (
                    val if isinstance(val, dict) else {"result": val}
                )
                completed_evt = Event.create(
                    event_type=f"tool.{tool_name}.completed",
                    agent_id=request_event.agent_id,
                    event_class="domain",
                    causation_id=uuid.UUID(idempotency_key),
                    data=evt_data,
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
            logger.exception(
                "worker.tool.hard_crash",
                tool=tool_name,
                message_id=message_id,
                error=str(e),
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
                        data={"error": f"Max retries exceeded / Worker crash: {e!s}"},
                        correlation=request_event.correlation,
                    )
                    await self._event_log.append(failed_evt)
                    await self._redis.xack(stream_key, self._group_name, message_id)
                    # We could also write to a DLQ stream here if needed.

    async def _reaper_loop(self, tool_name: str) -> None:
        """Periodically scans PEL and re-claims stuck messages (auto-recovery)."""
        stream_key = self._stream_key(tool_name)
        # Idle time is in milliseconds for redis
        idle_time_ms = int(self._reaper_idle_time * 1000)
        # O semáforo é criado em ``_consume_loop`` quando o loop sobe.
        # Antes do consume_loop estar pronto, o reaper acorda com sem=None
        # — nesse caso pulamos o reclaimer (o consume vai drenar tudo de
        # qualquer forma). Isso evita um TOCTOU entre start() e o loop
        # efetivo. Quando o semáforo existir, o reaper o honra.
        sem = self._semaphores.get(tool_name)

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
                # Re-resolve the semaphore aqui (não no topo do loop) porque
                # o ``_consume_loop`` pode subir depois do reaper — nesse
                # intervalo queremos usar None para skip.
                sem = self._semaphores.get(tool_name)
                for message_id, message_data in messages:
                    # By claiming, we become the owner. The delivery_count incremented.
                    # We process it immediately.
                    logger.warning(
                        "worker.reaper.reclaimed",
                        tool=tool_name,
                        message_id=message_id.decode(),
                    )

                    async def _process_with_reaper_sem(
                        _msg_id: str,
                        _msg_data: dict,
                        _sem: asyncio.Semaphore | None = sem,
                    ) -> None:
                        if _sem is not None:
                            async with _sem:
                                await self._process_message(
                                    tool_name, stream_key, _msg_id, _msg_data
                                )
                        else:
                            # Fallback enquanto o consume_loop não está
                            # ativo: processa direto. Aceita concorrência
                            # momentânea acima do limite, mas só durante
                            # o startup.
                            await self._process_message(
                                tool_name, stream_key, _msg_id, _msg_data
                            )

                    # Process message concurrently so reaper isn't blocked
                    asyncio.create_task(
                        _process_with_reaper_sem(message_id.decode(), message_data)
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(
                    "worker.reaper.error",
                    tool=tool_name,
                    error=str(e),
                )
                self._last_error = repr(e)
