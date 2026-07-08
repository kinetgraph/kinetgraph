# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Runner — the side-effecting counterpart of pure systems.

The runner is the ONLY component in FMH that touches the event log
for write purposes (other than adapters that emit external
intentions). It owns the tick loop.

Tick loop (simplified)
----------------------

  while running:
      world = await fold_world(log)                 # pure replay
      new_events = []
      for cyclic in self.cyclic_systems:            # pure
          new_events.extend(cyclic(world))
      result = await log.append_batch(new_events)   # side effect
      tick += 1
      await asyncio.sleep(interval)

Idempotency
-----------
`log.append_batch` is idempotent. Re-running the runner on the
same world does NOT duplicate events because each event_id is
deterministic and the EventLog skips duplicates.

Eventually-consistent
---------------------
The runner does NOT synchronously fold-and-apply. It does the
fold at tick T and applies the resulting events at tick T+1. So
a system that "decides" something only sees the world as it was
when the tick started. This is the documented eventual consistency
of the model: the system is correct given the snapshot it sees;
its effects apply in the next tick.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Optional

import structlog

from ..core.event import Event
from ..core.system import CyclicSystem
from ..core.world import World
from ..stream.event_log import EventLog
from ..stream.projection import fold_world

logger = structlog.get_logger()


class Runner:
    """
    Polling-driven runner.

    Usage:

        runner = Runner(
            log=event_log,
            cyclic_systems=[validate_documents, kick_idle_agents],
            tick_interval=1.0,
        )
        await runner.start()
        ...later...
        await runner.stop()
    """

    def __init__(
        self,
        log: EventLog,
        *,
        cyclic_systems: Optional[list[CyclicSystem]] = None,
        tick_interval: float = 1.0,
        fold: Optional[Callable[[], "asyncio.Future[World]"]] = None,
    ) -> None:
        self._log = log
        self._systems: list[CyclicSystem] = list(cyclic_systems or [])
        self._interval = tick_interval
        self._fold = fold or (lambda: fold_world(self._log))
        self._running = False
        self._tick = 0
        self._task: Optional[asyncio.Task] = None

    @property
    def tick(self) -> int:
        return self._tick

    @property
    def systems(self) -> list[CyclicSystem]:
        return list(self._systems)

    def add_cyclic_system(self, system: CyclicSystem) -> None:
        self._systems.append(system)

    async def tick_once(self) -> int:
        """
        Runs a single tick: replay + apply systems + append.

        Returns the new tick number. Safe to call directly from
        tests.
        """
        # 1. Pure replay
        world = await self._fold()
        # 2. Apply cyclic systems (pure)
        new_events: list[Event] = []
        for sys in self._systems:
            out = sys(world)
            # Accept both sync returns and awaitables
            if not isinstance(out, list):
                out = await out
            new_events.extend(out)
        # 3. Append (idempotent side effect)
        if new_events:
            result = await self._log.append_batch(new_events)
            if result.is_err():
                logger.error(
                    "runner.tick.append_failed",
                    tick=self._tick,
                    error=str(result.err_value()),
                )
                return self._tick
        self._tick += 1
        logger.debug("runner.tick.done", tick=self._tick, produced=len(new_events))
        return self._tick

    async def start(self) -> None:
        """Starts the tick loop. Idempotent."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="fmh-runner")
        logger.info(
            "runner.start", tick_interval=self._interval, systems=len(self._systems)
        )

    async def stop(self) -> None:
        """Stops the tick loop and waits for the current tick to finish."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("runner.stop", final_tick=self._tick)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick_once()
            except Exception as e:
                logger.error("runner.loop.error", error=str(e))
            await asyncio.sleep(self._interval)
