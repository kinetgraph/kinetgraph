# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Worker Manager - orchestrates the Tool Worker Pattern (ADR-036).
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Type

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kntgraph.infra.redis import RedisLike

from kntgraph.core.event import Event
from kntgraph.stream.event_log.store import EventLog

logger = logging.getLogger(__name__)


def _invoke_tool_sync(tool_cls: Type, idempotency_key: str, kwargs: dict) -> dict:
    """
    Synchronous wrapper to run the tool in a separate process.
    We instantiate the tool and run its async invoke method using a local event loop.
    Returns the Result serialized as a dict to pass back via multiprocessing.
    """
    tool_instance = tool_cls()

    # We create a new event loop for this process/invocation
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            tool_instance.invoke(idempotency_key=idempotency_key, **kwargs)
        )
        if result.is_ok():
            return {"status": "ok", "value": result.unwrap()}
        else:
            return {"status": "err", "error": str(result.err_value_or_raise())}
    finally:
        loop.close()


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
    ):
        self._redis = redis
        self._event_log = event_log
        self._group_name = group_name
        self._consumer_name = consumer_name

        self._reaper_interval = reaper_interval
        self._reaper_idle_time = reaper_idle_time

        self._tools: dict[str, Type] = {}
        self._pool: ProcessPoolExecutor | None = None

        self._running = False
        self._tasks: list[asyncio.Task] = []

    def register(self, tool_cls: Type) -> None:
        """Register a class decorated with @tool_worker."""
        if not hasattr(tool_cls, "name"):
            raise TypeError("Tool must be decorated with @tool_worker")
        self._tools[tool_cls.name] = tool_cls

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

        self._pool = ProcessPoolExecutor(max_workers=max_workers)

        for tool_name in self._tools:
            # Ensure Consumer Group exists
            stream_key = f"fmh:tools:{tool_name}:queue"
            try:
                await self._redis.xgroup_create(
                    stream_key, self._group_name, id="0", mkstream=True
                )
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    logger.error(f"Failed to create group for {tool_name}: {e}")

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
        stream_key = f"fmh:tools:{tool_name}:queue"
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
                    continue

                for _, messages in response:
                    for message_id, message_data in messages:
                        await self._process_message(
                            tool_name, stream_key, message_id.decode(), message_data
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in consume loop for {tool_name}: {e}")
                await asyncio.sleep(1)

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
            logger.error(f"Failed to parse payload for {message_id}: {e}")
            await self._redis.xack(stream_key, self._group_name, message_id)
            return

        tool_params = (
            request_event.data.get("params") or request_event.data.get("args") or {}
        )
        idempotency_key = str(request_event.event_id)

        try:
            # We use asyncio.get_running_loop().run_in_executor to run the tool synchronously
            # in a separate process. The wrapper _invoke_tool_sync will handle the asyncio loop inside the process.
            loop = asyncio.get_running_loop()
            result_dict = await loop.run_in_executor(
                self._pool, _invoke_tool_sync, tool_cls, idempotency_key, tool_params
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

            # Acknowledge the message since it was processed (success or explicit failure)
            await self._redis.xack(stream_key, self._group_name, message_id)

        except Exception as e:
            # A hard crash (e.g. process died, OOM, exception in invoke outside Result)
            logger.error(f"Tool execution hard-crashed for {message_id}: {e}")

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
                        f"DLQ triggered for {message_id} after {delivery_count} attempts."
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
        stream_key = f"fmh:tools:{tool_name}:queue"
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
                        f"Reclaimed stuck message {message_id.decode()} for {tool_name}"
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
                logger.error(f"Reaper loop error for {tool_name}: {e}")
