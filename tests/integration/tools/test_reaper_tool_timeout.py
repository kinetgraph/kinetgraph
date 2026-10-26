# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end: a slow tool is not reclaimed while it runs
(ADR-085 §3.2, issue #83).

The bug this covers was measured against a live Redis on
2026-10-25: a tool slower than the global ``reaper_idle_time``
was reclaimed **while it still ran**, and the same request
executed twice, in parallel.

The mechanism needs a real Redis: the reaper reads the PEL
(``XPENDING``) for the idle time and the stream (``XRANGE``)
for the payload, then claims with ``XAUTOCLAIM``. The unit
tests mock all three; this one uses the real commands.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter
from kntgraph.tools.worker import tool_worker

pytestmark = pytest.mark.asyncio


@tool_worker(name="slowpoke", max_concurrency=1, timeout_seconds=900)
class _SlowTool:
    """A tool that legitimately takes longer than the global
    ``reaper_idle_time``."""

    async def invoke(self, *, idempotency_key: str) -> dict:
        await asyncio.sleep(0.5)
        return {"done": True}


async def _prime_pending(redis, *, stream_key: str, payload: bytes) -> None:
    """Put one message on the queue and deliver it to the
    consumer group, so it sits in the PEL un-ACKed."""
    await redis.xadd(stream_key, {"payload": payload})
    await redis.xgroup_create(stream_key, "fmh_tool_workers", id="0", mkstream=True)
    await redis.xreadgroup(
        groupname="fmh_tool_workers",
        consumername="w1",
        streams={stream_key: ">"},
        count=1,
    )


async def test_a_slow_tool_is_not_reclaimed_while_it_runs(clean_redis):
    """The tool declares 900 s; the global fallback is 300 s.

    The message sits in the PEL, un-ACKed, as if a slow tool
    were running. The reaper must leave it alone — reclaiming
    it is what made the same request execute twice, in
    parallel.
    """
    from kntgraph.infra.redis._event_log import RedisEventLogAdapter
    from kntgraph.stream.event_log import EventLog

    log = EventLog(RedisEventLogAdapter(clean_redis))
    manager = WorkerManager(
        clean_redis,
        log,
        reaper_idle_time=300.0,
    )
    manager.register(_SlowTool, acl=None)

    agent_id = f"a-{uuid.uuid4()}"
    request_event = Event.create(
        event_type="tool.slowpoke.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "slowpoke", "params": {}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )
    stream_key = "knt:tools:slowpoke:queue"
    await _prime_pending(
        clean_redis,
        stream_key=stream_key,
        payload=request_event.to_json().encode(),
    )

    await manager._reaper_loop_once("slowpoke")  # type: ignore[attr-defined]

    # The message must still be in the PEL, owned by the
    # consumer that has it — not claimed by the reaper.
    pending = await clean_redis.xpending_range(
        stream_key, "fmh_tool_workers", min="-", max="+", count=10
    )
    assert len(pending) == 1, "the reaper reclaimed the running tool"
    assert pending[0]["consumer"] == b"w1"

    # And the queue still holds exactly one message — the
    # request was not duplicated.
    assert await clean_redis.xlen(stream_key) == 1


async def test_the_router_writes_the_request_unchanged(clean_redis):
    """The router adds nothing to the request.

    The deadline is derived by the reaper from the request's own
    timestamp, so the router has no deadline to carry.
    """
    router = ToolRouter(clean_redis)
    agent_id = f"a-{uuid.uuid4()}"
    request_event = Event.create(
        event_type="tool.slowpoke.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "slowpoke", "params": {}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )

    await router.route_batch([request_event])

    rows = await clean_redis.xrange("knt:tools:slowpoke:queue", min="-", max="+")
    assert len(rows) == 1
    payload = rows[0][1][b"payload"]
    decoded = json.loads(payload)
    assert decoded == json.loads(request_event.to_json())
    # The timestamp the reaper derives the deadline from is there.
    assert decoded["timestamp"] == request_event.timestamp.isoformat()
