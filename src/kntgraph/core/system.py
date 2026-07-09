# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
System — a pure function from World to list[Event].

In FMH v2.2, every system is the same shape:

    System: (World) -> list[Event]

The framework invokes each registered system once per tick
(after the incremental World fold for reactive systems, after
the schedule tick for cyclic systems). Systems do NOT receive
the triggering event — they inspect the World's components
(via ``world.query_agents(MyComponent)``) and emit events based
on the business rules they encode.

Two dispatchers drive ticks:

  - ``ReactiveDispatcher``: tick when the EventLog has new events
    for a tracked agent. Folds incrementally using
    ``World.with_event``.
  - ``Runner``: tick on a schedule (every N seconds). Builds a
    fresh World via ``fold_world``.

Both fold into the same World and call the same systems. The
distinction is purely about cadence, not signature.

Backwards compat
----------------

The aliases ``ReactiveSystem`` and ``CyclicSystem`` are kept
for legacy imports. They share the same signature
(``(World) -> list[Event]``). Systems written against the
v2.0/2.1 ``ReactiveSystem(world, event)`` signature will
break at runtime — they need to drop the event parameter
and use ``world.query_agents(...)`` instead. See ADR-018.

See: ADR-018 — WorldIncremental + WorldSystem.

Properties of every system:

  - deterministic (same world + same system = same output)
  - testable in isolation (construct an in-memory world, call system)
  - replayable (re-running a system on the same world is a no-op)
  - composable (dispatcher pipelines any list of systems)
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from .event import Event
    from .world import World

SystemReturn = typing.Union[list["Event"], typing.Awaitable[list["Event"]]]


@typing.runtime_checkable
class WorldSystem(typing.Protocol):
    """
    A pure function from World to list[Event].

    The framework invokes each registered system once per
    tick (after the incremental World fold). Systems inspect
    the World's components via ``world.query_agents(...)``
    and emit events based on the rules they encode. They do
    NOT receive the triggering event — the World already
    captures its effect via the projection.

    Implementations should be PURE — no I/O, no side effects.
    If a system needs to talk to the outside world, emit an
    event (``call_external_api.requested``) that a SEPARATE
    adapter (an I/O system) consumes out-of-band and turns
    back into an event.

    The system MAY be ``async`` if the application prefers
    that style (it composes more easily with the dispatcher's
    async tick loop). A sync function is also acceptable.
    """

    def __call__(self, world: "World") -> SystemReturn: ...


# Backwards-compat aliases. New code should use ``WorldSystem``.
# ``ReactiveSystem`` and ``CyclicSystem`` are now interchangeable
# at the type level — both are ``(World) -> list[Event]``. The
# distinction (event-driven vs sweep) was an implementation
# detail of the old dispatcher; the new incremental dispatcher
# folds then runs all systems once per tick.
ReactiveSystem = WorldSystem
CyclicSystem = WorldSystem

# Type aliases for plain callables.
System = typing.Callable[["World"], SystemReturn]
Reactive = typing.Callable[["World"], SystemReturn]
Cyclic = typing.Callable[["World"], SystemReturn]

__all__ = [
    "Cyclic",
    "CyclicSystem",
    "Reactive",
    "ReactiveSystem",
    "System",
    "WorldSystem",
]
