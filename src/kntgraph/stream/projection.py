# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Stream projection — read paths from the EventLog to the World.

The World is a *fold* of the event stream. The functions here
build that fold, optionally restricted to a single agent or to a
subset of events. The default projection is `project_default` from
`core.world`.

This module is the bridge between Redis-backed storage and the
in-memory World. It does NOT mutate Redis; it only reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from ..core.event import Event
from ..core.world import Projection, World, project_default
from .event_log import EventLog


async def read_all_events(log: EventLog) -> list[Event]:
    """
    Read every event from every agent stream. NOT recommended in
    production for large deployments; intended for tests and
    small-scale recovery.
    """
    out: list[Event] = []
    async for e in log.iter_all():
        out.append(e)
    return out


async def fold_world(
    log: EventLog,
    *,
    agent_ids: Optional[list[str]] = None,
    projection: Projection = project_default,
    tick: Optional[int] = None,
) -> World:
    """
    Build a World by reading events from the log and folding them.

    `agent_ids` optionally restricts the fold to a subset of agents.
    `projection` defaults to the framework's `project_default`.
    `tick` is set on the resulting World (default: number of agents
    folded, which is a poor default; callers should pass the
    intended tick).
    """
    events: list[Event] = []
    if agent_ids is None:
        async for e in log.iter_all():
            events.append(e)
    else:
        for aid in agent_ids:
            events.extend(await log.read(aid))
    return World.fold(events, projection=projection, tick=tick)


async def fold_world_for_agent(
    log: EventLog,
    agent_id: str,
    *,
    projection: Projection = project_default,
) -> World:
    """
    Build a World by folding ONLY one agent's history. Useful for
    reactive dispatch (a system that needs to inspect a single
    agent's current state).
    """
    events: Sequence[Event] = await log.read(agent_id)
    return World.fold(list(events), projection=projection)


async def stream_agents(log: EventLog) -> list[str]:
    """Returns the list of agent_ids that have at least one event."""
    return await log.list_agents()
