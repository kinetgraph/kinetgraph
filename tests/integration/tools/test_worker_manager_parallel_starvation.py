# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration test for the parallel-tool starvation bug.

The bug
-------
``WorkerManager`` invokes each tool via
``asyncio.to_thread(_invoke_tool_sync)``, where
``_invoke_tool_sync`` creates a fresh event loop, runs the
tool's ``invoke`` coroutine, and closes the loop. When two
tools run in parallel, the fast one can be starved by the
slow one: ``asyncio.sleep(0.05)`` returns after hundreds of
milliseconds in the WorkerManager environment, pushing the
fast tool's completion past the 0.5s test poll window used
by ``test_fsm_non_blocking_interleaved_tools``.

The bug is a fakeredis-specific artifact (in-process
serialised locks + a fresh event loop in a worker thread
starve the fast tool's ``asyncio.sleep``); against a real
Redis the same code path passes 5/5. The reproduction is
kept under ``tests/integration/`` so the fakeredis flake
does not block the unit-test CI gate, and so the bug is
still pinned for any future fix.

This test amplifies the symptom by re-running the parallel
slow+fast pair 8 times in a row; the median and max
fast-tool sleeps pin the starvation.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Ok, Result, ToolError
from kntgraph.core.world import DomainComponent
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.stream.event_log import EventLog
from kntgraph.tools import tool_worker
from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.router import ToolRouter

pytestmark = [pytest.mark.asyncio]


@dataclass(frozen=True, slots=True)
class _FlakeProbe(DomainComponent):
    slept: float = 0.0


def _record(name: str, sleeps: dict[str, float], t0: float) -> float:
    slept = time.monotonic() - t0
    sleeps[name] = slept
    return slept


async def test_worker_manager_does_not_starve_fast_tool(
    clean_redis: Any,
) -> None:
    """Pin the starvation. The fast tool must finish <200ms.

    We run the slow+fast pair 8 times in a row to amplify
    the intermittent starvation. The slow tool sleeps 300ms
    (6x the fast target). If the WorkerManager serialises
    the two ``to_thread`` calls instead of running them in
    parallel, the fast tool would finish at ~350ms on the
    serialised path, well above the 200ms bound.

    Failure of the max assertion means at least one
    iteration saw the fast tool starve. Failure of the
    median assertion means the starvation is the dominant
    case (the framework's per-invocation loop-open/close
    strategy is unfit for the WorkerManager's per-tool
    latency budget).

    Requires the real Redis instance wired by
    ``tests/integration/conftest.py``. The test is skipped
    automatically if Redis is not reachable.
    """
    n_iter = 8
    slow_dur = 0.3
    fast_dur = 0.05
    max_allowed = 0.2  # 4x the nominal fast sleep

    @tool_worker(name="flake_slow_tool", max_concurrency=4, retries=0)
    class SlowTool:
        async def invoke(
            self, *, idempotency_key: str, state: str = "", **kwargs: Any
        ) -> Result[dict, ToolError]:
            t0 = time.monotonic()
            await asyncio.sleep(slow_dur)
            _record("slow", last_sleeps, t0)
            return Ok({"status": "slow_done"})

    @tool_worker(name="flake_fast_tool", max_concurrency=4, retries=0)
    class FastTool:
        async def invoke(
            self, *, idempotency_key: str, state: str = "", **kwargs: Any
        ) -> Result[dict, ToolError]:
            t0 = time.monotonic()
            await asyncio.sleep(fast_dur)
            _record("fast", last_sleeps, t0)
            return Ok({"status": "fast_done"})

    fast_samples: list[float] = []

    for i in range(n_iter):
        last_sleeps: dict[str, float] = {}
        log = EventLog(RedisEventLogAdapter(client=clean_redis))
        router = ToolRouter(clean_redis)
        wm = WorkerManager(
            redis=clean_redis,
            event_log=log,
            reaper_interval=5.0,
            reaper_idle_time=15.0,
        )
        wm.register(SlowTool, acl=None)
        wm.register(FastTool, acl=None)

        agent_id = f"flake-{uuid.uuid4()}"
        corr = CorrelationContext.new()
        for tool_name in ("flake_slow_tool", "flake_fast_tool"):
            evt = Event.create(
                event_type=f"tool.{tool_name}.requested",
                agent_id=agent_id,
                event_class="domain",
                data={"job": tool_name, "i": i},
                correlation=corr,
                producer_principal_id="probe",
            )
            await log.append(evt)
            await router.route_batch([evt])

        await wm.start()
        try:
            for _ in range(60):
                events = await log.read(agent_id)
                completed = {
                    e.event_type
                    for e in events
                    if e.event_type.startswith("tool.")
                    and e.event_type.endswith(".completed")
                }
                if {
                    "tool.flake_slow_tool.completed",
                    "tool.flake_fast_tool.completed",
                } <= completed:
                    break
                await asyncio.sleep(0.05)
        finally:
            await wm.stop()

        assert "fast" in last_sleeps, f"iter {i}: fast tool never completed"
        assert "slow" in last_sleeps, f"iter {i}: slow tool never completed"
        fast_samples.append(last_sleeps["fast"])

    sorted_fast = sorted(fast_samples)
    median_fast = sorted_fast[len(sorted_fast) // 2]
    max_fast = max(fast_samples)
    assert median_fast < max_allowed, (
        f"Median fast-tool sleep across {n_iter} parallel runs is "
        f"{median_fast * 1000:.1f}ms; expected <{max_allowed * 1000:.0f}ms. "
        f"all samples: {[f'{x * 1000:.1f}ms' for x in fast_samples]}"
    )
    assert max_fast < max_allowed, (
        f"Max fast-tool sleep across {n_iter} parallel runs is "
        f"{max_fast * 1000:.1f}ms; expected <{max_allowed * 1000:.0f}ms. "
        f"Starvation reproduced. "
        f"all samples: {[f'{x * 1000:.1f}ms' for x in fast_samples]}"
    )
