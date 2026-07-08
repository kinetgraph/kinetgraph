# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
World — a pure projection of the event stream at a given tick.

In FMH v2.0, the World is a *derived* value. It is what you get when
you fold the events up to tick T through their projection function.

  World :: Tick -> Sequence[Event] -> World

The Redis Stream is the only source of truth. Every tick the runner
re-builds (or replays) the world from the stream. Systems are pure
functions of the world — they take it in and return new events to
append.

This module is a thin facade. The implementation is split across
the `world` subpackage:

  - `world.view`        — `AgentView` (the per-agent immutable
    snapshot).
  - `world.projection`  — the default fold (`project_default`,
    `_apply_event`), the `Projection` type alias, and the
    lifecycle-event-to-phase mapper (which reuses
    `event.operational.OPERATIONAL_EVENT_TO_PHASE`).
  - `world.world`       — the `World` class (snapshot at tick T).
  - `world.query`       — `WorldQuery` (the unified query type;
    the previous `FilteredWorldQuery` was collapsed into it
    via predicate composition).

State storage:
  - `ArchetypeStorage` is the working set.
  - The World facade is a thin wrapper that exposes query/update ops
    on top of the storage. Mutation returns a new World (copy-on-write
    via the storage's own internal copy methods).

What is NOT in the World anymore (v1.x → v2.0):

  - `status` field on AgentState  → replaced by LifecycleComponent
                                    in components, derived from
                                    the agent's last "lifecycle" event.
  - `pending_events` on AgentState → moved out; events are appended
                                    to the stream by the runner.
  - `outbox` on World              → removed; the runner holds the
                                    "events to append" buffer.
  - `repository.save_world/get_world` → removed; the World is rebuilt
                                    from the stream, never persisted
                                    as a whole.

Backwards-compatibility note: `FilteredWorldQuery` was a parallel
class to `WorldQuery` that filtered by an arbitrary predicate.
After the split, the two are unified: `WorldQuery.filter()` returns
the same type, and the predicate chain is composed. Call sites that
imported `FilteredWorldQuery` directly (no public consumer does)
will see an `ImportError`; they should use `WorldQuery.filter()`
or construct `WorldQuery(world, predicate=fn)`.
"""

from .projection import Projection, project_default
from .query import WorldQuery
from .view import AgentView
from .world import World

__all__ = [
    "AgentView",
    "Projection",
    "World",
    "WorldQuery",
    "project_default",
]
