# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Checkpoint and EventLog I/O helpers for the reactive dispatcher.

The three I/O-bound operations that the per-agent tick
touches in Redis live here so the dispatcher's tick body
stays flat. They are thin wrappers over the public
``EventLog`` and ``IncrementalWorldStore`` APIs (ADR-019):
the dispatcher no longer reaches through ``self._log._redis``
to enumerate agents or read ranges.

The functions are module-level rather than methods so
this module can be unit-tested in isolation (no systems,
no fold, just the I/O round-trip with fakes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kntgraph.core.event import Event
from kntgraph.infra.world_checkpoint import WorldCheckpoint

if TYPE_CHECKING:
    from kntgraph.core.world import World
    from kntgraph.runner.reactive import ReactiveDispatcher


__all__ = ["save_checkpoint", "bootstrap_agents", "fetch_new_events"]


async def save_checkpoint(
    dispatcher: "ReactiveDispatcher",
    agent_id: str,
    world: "World",
    last_stream_id: str,
) -> None:
    """Persist the World checkpoint.

    Always called, even when ``new_event_count == 0``,
    so the cursor advances past fully-filtered batches.
    """
    await dispatcher._world_store.save(
        agent_id,
        WorldCheckpoint(
            world=world,
            last_stream_id=last_stream_id,
        ),
    )


async def bootstrap_agents(dispatcher: "ReactiveDispatcher") -> None:
    """Initial discovery of agents. Called on the first
    dispatch and again on the rediscovery cadence. After
    bootstrap, the dispatcher iterates only
    ``self._tracked_agents``.

    Iteration 5 (ADR-019): uses ``EventLog.list_agents``
    (the public delegation added in this iteration)
    instead of the legacy private ``_list_agent_ids``.
    The dispatcher no longer reaches through
    ``self._log._redis`` to enumerate agents.
    """
    agent_ids = await dispatcher._log.list_agents()
    for aid in agent_ids:
        dispatcher._tracked_agents.add(aid)


async def fetch_new_events(
    dispatcher: "ReactiveDispatcher",
    agent_id: str,
    cursor: str,
) -> tuple[list[Event], str]:
    """Read events for one agent STRICTLY AFTER ``cursor``.

    Returns parsed ``Event`` objects. Iteration 5
    (ADR-019): uses the public
    ``EventLog.read_after_cursor`` instead of the
    legacy ``self._log._redis.xrange(...)`` direct
    access.
    """
    return await dispatcher._log.read_after_cursor(agent_id, cursor)
