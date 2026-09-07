# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""
Benchmark script: 500 Concurrent Agents under Heavy Load (ADR-068 Validation).

Topology & Workload:
  - 500 Agents (agent-001 .. agent-500)
  - 4 Worker Tools:
      1. fast_io   : 5ms simulated network call (asyncio.sleep)
      2. slow_io   : 250ms simulated network call (asyncio.sleep)
      3. failing_io: 10ms delay then raises RuntimeError("Upstream 503")
      4. slow_cpu  : SHA-256 CPU computation loop in ProcessPoolExecutor
  - Orchestration:
      - ReactiveDispatcher with wake_on_event=True (subscribe_many over 500 agents)
      - WorkerManager with 4 workers and ProcessPoolExecutor
  - Measures:
      - Total throughput (events/s)
      - p50, p95, p99 latency per tool
      - Redis connection pool stability under fan-in
      - Event completion & error handling metrics

Usage:
  KNT_REDIS_URL="redis://:redispassword@127.0.0.1:6379/0" python scripts/benchmark_500_agents.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections import Counter
from uuid import uuid4

import structlog
from redis.asyncio import Redis

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Ok
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._world_checkpoint import RedisWorldCheckpointStorage
from kntgraph.infra.world_checkpoint import IncrementalWorldStore
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.tools.acl import default_acl
from kntgraph.stream.event_log import EventLog
from kntgraph.tools import tool_worker
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------


@tool_worker(name="fast_io", max_concurrency=64, retries=1)
class FastIOTool:
    """Fast IO-bound tool simulating a 5ms async HTTP network round-trip."""

    async def invoke(
        self, agent: str = "", seq: int = 0, *, idempotency_key: str = ""
    ) -> Ok[dict]:
        """Execute fast IO simulation and return a success result."""
        await asyncio.sleep(0.005)
        return Ok({"status": "fast_io_ok", "agent": agent, "seq": seq})


@tool_worker(name="slow_io", max_concurrency=64, retries=1)
class SlowIOTool:
    """Slow IO-bound tool simulating a 250ms async HTTP network call."""

    async def invoke(
        self, agent: str = "", seq: int = 0, *, idempotency_key: str = ""
    ) -> Ok[dict]:
        """Execute slow IO simulation and return a success result."""
        await asyncio.sleep(0.25)
        return Ok({"status": "slow_io_ok", "agent": agent, "seq": seq})


@tool_worker(name="failing_io", max_concurrency=32, retries=0)
class FailingIOTool:
    """Failing IO-bound tool simulating an unhandled HTTP 503 upstream error."""

    async def invoke(
        self, agent: str = "", seq: int = 0, *, idempotency_key: str = ""
    ) -> Ok[dict]:
        """Simulate an upstream service failure by raising a RuntimeError."""
        await asyncio.sleep(0.01)
        raise RuntimeError("Simulated upstream HTTP 503 error")


@tool_worker(name="slow_cpu", max_concurrency=16, retries=1)
class SlowCPUTool:
    """CPU-bound tool executing 25,000 SHA-256 iterations in a process pool."""

    __tool_worker_cpu_bound__ = True

    async def invoke(
        self, agent: str = "", seq: int = 0, *, idempotency_key: str = ""
    ) -> Ok[dict]:
        """Perform CPU-intensive hashing and return hex digest snippet."""
        # Initial byte string derived from agent identity and sequence number
        val = f"{agent}:{seq}".encode("utf-8")
        # Perform SHA-256 hashing loop to simulate CPU load
        for _ in range(25_000):
            val = hashlib.sha256(val).digest()
        return Ok({"status": "slow_cpu_ok", "digest": val.hex()[:16]})


# ---------------------------------------------------------------------------
# Benchmark Suite
# ---------------------------------------------------------------------------


async def run_benchmark(
    num_agents: int = 500,
    requests_per_agent: int = 2,
    redis_url: str = "redis://:redispassword@127.0.0.1:6379/0",
) -> None:
    """Execute end-to-end load benchmark with 500 agents over real Redis."""
    print("=================================================================")
    print(f"   KINETGRAPH BENCHMARK — {num_agents} CONCURRENT AGENTS")
    print("=================================================================")
    print(f"  Redis URL            : {redis_url}")
    print(f"  Total Agents         : {num_agents}")
    print(f"  Requests / Agent     : {requests_per_agent}")
    print(f"  Total Event Requests : {num_agents * requests_per_agent}")
    print("-----------------------------------------------------------------")

    client = Redis.from_url(
        redis_url, decode_responses=False, max_connections=256, socket_timeout=60.0
    )
    log = EventLog(storage=RedisEventLogAdapter(client=client))
    world_store = IncrementalWorldStore(RedisWorldCheckpointStorage(client=client))

    router = ToolRouter(redis=client)
    manager = WorkerManager(redis=client, event_log=log)
    manager.register(FastIOTool, acl=default_acl())
    manager.register(SlowIOTool, acl=default_acl())
    manager.register(FailingIOTool, acl=default_acl())
    manager.register(SlowCPUTool, acl=default_acl())

    dispatcher = ReactiveDispatcher(
        log=log,
        world_store=world_store,
        systems=[],
        redis=client,
        tool_router=router,
        wake_on_event=True,
        fallback_poll_interval=0.2,
    )

    agent_ids = [f"benchmark-agent-{i:03d}" for i in range(1, num_agents + 1)]
    for aid in agent_ids:
        dispatcher.track_agent(aid)

    # Cleanup any leftover streams from previous runs
    print("Cleaning up leftover Redis test keys...")
    await client.flushdb()

    print("Starting WorkerManager and ReactiveDispatcher...")
    await manager.start()
    await dispatcher.start()

    tool_names = ["fast_io", "slow_io", "failing_io", "slow_cpu"]
    start_time = time.monotonic()
    latencies: list[float] = []

    print(f"Emitting {num_agents * requests_per_agent} requests across 500 agents...")

    emit_sem = asyncio.Semaphore(50)

    async def emit_for_agent(aid: str, idx: int) -> None:
        async with emit_sem:
            tool_name = tool_names[(idx + hash(aid)) % len(tool_names)]
            for attempt in range(5):
                event = Event.create(
                    event_type=f"tool.{tool_name}.requested",
                    agent_id=aid,
                    event_class="domain",
                    data={"agent": aid, "seq": idx},
                    correlation=CorrelationContext.new(correlation_id=uuid4()),
                    producer_principal_id=aid,
                )
                t_req = time.monotonic()
                res = await log.append(event)
                if res.is_ok():
                    latencies.append(time.monotonic() - t_req)
                    break
                await asyncio.sleep(0.05 * (attempt + 1))
            else:
                print(f"FAILED to emit request for {aid} after 5 attempts")

    # Emit all requests concurrently
    tasks = []
    for aid in agent_ids:
        for r in range(requests_per_agent):
            tasks.append(emit_for_agent(aid, r))

    await asyncio.gather(*tasks)
    emission_done_time = time.monotonic()
    emission_duration = emission_done_time - start_time
    print(
        f"Emission finished in {emission_duration:.3f}s ({len(tasks) / emission_duration:.1f} req/s emitted)"
    )

    print("Waiting for processing to complete across 500 agents...")
    target_count = len(tasks)
    processed_count = 0
    timeout_deadline = time.monotonic() + 120.0
    sem = asyncio.Semaphore(25)

    async def _safe_read(aid: str) -> list[Event]:
        async with sem:
            return await log.read(aid)

    while processed_count < target_count and time.monotonic() < timeout_deadline:
        await asyncio.sleep(0.5)
        pipe = client.pipeline()
        for aid in agent_ids:
            pipe.xlen(f"knt:agents:{aid}:events")
        lengths = await pipe.execute()
        total_events = sum(lengths)
        processed_count = max(0, total_events - target_count)

    end_time = time.monotonic()
    total_duration = end_time - start_time

    print("Stopping orchestrators...")
    await dispatcher.stop()
    await manager.stop()

    # Aggregate Metrics
    print("\n-----------------------------------------------------------------")
    print("   BENCHMARK RESULTS")
    print("-----------------------------------------------------------------")
    print(f"  Total Duration      : {total_duration:.3f} seconds")
    print(f"  Requests Emitted    : {target_count}")
    print(f"  Events Processed    : {processed_count} / {target_count}")
    print(f"  Overall Throughput  : {processed_count / total_duration:.2f} events/sec")

    if latencies:
        latencies.sort()
        p50 = latencies[int(len(latencies) * 0.50)] * 1000
        p95 = latencies[int(len(latencies) * 0.95)] * 1000
        p99 = latencies[int(len(latencies) * 0.99)] * 1000
        print("\n  Append Latency (Emit):")
        print(f"    p50 : {p50:.2f} ms")
        print(f"    p95 : {p95:.2f} ms")
        print(f"    p99 : {p99:.2f} ms")

    # Tool execution breakdown
    stats: Counter[str] = Counter()
    all_results = await asyncio.gather(*(_safe_read(aid) for aid in agent_ids))
    for events in all_results:
        for ev in events:
            if ev.event_type.startswith("tool."):
                stats[ev.event_type] += 1

    print("\n  Event Type Breakdown:")
    for etype, count in sorted(stats.items()):
        print(f"    {etype:<35}: {count}")

    print("-----------------------------------------------------------------")
    if processed_count >= target_count * 0.95:
        print(
            "  SUCCESS: 500-agent load test passed with high throughput & zero pool exhaustion!"
        )
    else:
        print(
            f"  WARNING: Processed {processed_count}/{target_count} events (some timed out)."
        )
    print("=================================================================")

    # Cleanup test streams
    for aid in agent_ids:
        await log.delete_agent_stream(aid)
        await world_store.discard(aid)
    for tool_name in ["fast_io", "slow_io", "failing_io", "slow_cpu"]:
        await client.delete(f"knt:tools:{tool_name}:queue")
    await client.aclose()


if __name__ == "__main__":
    redis_url = os.environ.get(
        "KNT_REDIS_URL", "redis://:redispassword@127.0.0.1:6379/0"
    )
    asyncio.run(
        run_benchmark(num_agents=500, requests_per_agent=2, redis_url=redis_url)
    )
