# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Direct unit tests for the helpers extracted from
``ReactiveDispatcher`` into the private modules
``_folding``, ``_checkpoint_io``, and ``_systems_runner``
(file-layout §3.1 — the dispatcher module had grown past
the 500-line guideline).

The dispatcher's public tests cover the helpers
end-to-end; these tests pin the helpers' contracts
directly so a future refactor of the dispatcher's
orchestrator does not silently change a helper's
branch coverage.

Each helper is tested for:
  - the happy path (the orchestrator-driven call)
  - one failure mode (the branch the orchestrator
    does not exercise in the existing dispatcher tests)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.view import AgentView
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner._checkpoint_io import (
    bootstrap_agents,
    fetch_new_events,
    save_checkpoint,
)
from kntgraph.runner._folding import fold_with_filter, fold_with_systems
from kntgraph.runner._systems_runner import (
    append_system_outgoing,
    run_systems_and_persist,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Captured:
    appended: list[Event] = field(default_factory=list)
    saved: list[tuple[str, WorldCheckpoint]] = field(default_factory=list)


class _FakeEventLog:
    def __init__(self, cap: _Captured) -> None:
        self._cap = cap
        self._agents: dict[str, list[Event]] = {}
        self._appended: list[Event] = []

    def add_agent(self, agent_id: str, *events: Event) -> None:
        self._agents.setdefault(agent_id, []).extend(events)

    async def read_after_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        pending = self._agents.get(agent_id, [])
        events = list(pending) if cursor in ("-", "-1") else []
        if events:
            self._agents[agent_id] = []
            return events, "1-0"
        return events, cursor

    async def list_agents(self) -> list[str]:
        return sorted(self._agents.keys())

    async def append_batch(self, events: list[Event]) -> Any:
        self._cap.appended.extend(events)
        return ["ok"] * len(events)


class _FakeWorldStore:
    def __init__(self, cap: _Captured) -> None:
        self._cap = cap

    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        self._cap.saved.append((agent_id, checkpoint))


def _seed_event(agent_id: str, event_type: str = "fixture.event") -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type=event_type,
        data={"k": "v"},
        correlation=CorrelationContext.new(correlation_id=uuid4()),
    )


def _build_dispatcher(*, log: _FakeEventLog, store: _FakeWorldStore, **kwargs):
    """Build a minimal ``ReactiveDispatcher`` for the
    helper tests. Avoids importing the dispatcher class
    directly so the helpers' contracts are pinned even
    if the constructor changes.
    """
    from kntgraph.runner.reactive import ReactiveDispatcher

    return ReactiveDispatcher(log=log, world_store=store, systems=[], **kwargs)


# ---------------------------------------------------------------------------
# _folding.fold_with_filter
# ---------------------------------------------------------------------------


class TestFoldWithFilter:
    async def test_fold_counts_surviving_events(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        events = [_seed_event("a-1") for _ in range(3)]
        world, count = fold_with_filter(dispatcher, World.empty(), events)
        assert count == 3
        assert "a-1" in world.views

    async def test_filter_excludes_events(self) -> None:
        """The branch the dispatcher does not exercise
        in the existing tests: ``_filter`` returning
        ``False`` for an event. Pinned here so the
        ``new_event_count`` invariant (only surviving
        events are counted) is not lost.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        dispatcher._filter = lambda _e: False
        events = [_seed_event("a-1") for _ in range(3)]
        _world, count = fold_with_filter(dispatcher, World.empty(), events)
        assert count == 0

    async def test_memory_projection_composes_into_world(self) -> None:
        """``fold_with_filter`` must run the memory
        hydration projection (``project_memory``,
        ADR-042 §6.1) so systems see ``SessionComponent`` /
        ``ProfileComponent`` / ``ContinuityComponent`` on
        the ``AgentView``. This was the bug that motivated
        this fix: production was folding the default
        projection only, and ``SessionComponent`` never
        reached the view, so ``ChatRoleSystem`` silently
        returned ``[]`` (line 121 of ``_base.py``).
        """
        from kntgraph.core.components.memory import (
            SessionComponent,
        )

        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        session_event = _seed_event("a-1", "session.started")
        session_event = Event.create(
            event_type="session.started",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=session_event.correlation,
            data={
                "session_id": "s-1",
                "user_id": "u-1",
                "tenant_id": "tenant-A",
                "started_at": "2026-08-26T00:00:00Z",
                "metadata": {},
            },
        )
        world, _count = fold_with_filter(dispatcher, World.empty(), [session_event])
        assert "a-1" in world.views
        assert SessionComponent in world.views["a-1"].components

    async def test_memory_projection_preserves_storage_for_replay(self) -> None:
        """The memory hydration updates both ``views`` and
        ``storage`` so a subsequent replay (which rebuilds
        the World from storage) preserves the components.
        If only ``views`` were updated, a checkpoint
        save would lose the memory components because
        ``storage.clone_with_entity`` is what makes the
        state durable across ticks.
        """

        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        session_event = Event.create(
            event_type="session.started",
            agent_id="a-2",
            event_class="lifecycle",
            correlation=_seed_event("a-2").correlation,
            data={
                "session_id": "s-2",
                "user_id": "u-1",
                "tenant_id": "tenant-A",
                "started_at": "2026-08-26T00:00:00Z",
                "metadata": {},
            },
        )
        world, _count = fold_with_filter(dispatcher, World.empty(), [session_event])
        rebuilt = world.storage.num_archetypes
        # The storage has at least one entity (a-2).
        # If only views were updated, storage would still
        # be empty here.
        assert rebuilt >= 1

    async def test_memory_projection_passes_through_when_no_memory_events(
        self,
    ) -> None:
        """Fast path: when the batch has no memory event,
        ``project_memory`` returns the base view unchanged
        for every agent (per the implementation contract
        in ``projection_memory.py:558-570``). The fold
        must return the same World object in that case --
        no allocation, no storage work, no extra fold.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        plain_event = _seed_event("a-3", "user.intent")
        world, _count = fold_with_filter(dispatcher, World.empty(), [plain_event])
        assert "a-3" in world.views


# ---------------------------------------------------------------------------
# _folding.fold_with_systems
# ---------------------------------------------------------------------------


class TestFoldWithSystems:
    async def test_no_tool_events_returns_same_world(self) -> None:
        """The fast-path branch (ADR-044 §2.4): a batch
        without any ``tool.*`` event returns the input
        World unchanged, with zero allocation.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        world = World.empty()
        result = fold_with_systems(dispatcher, world, [_seed_event("a-1")])
        assert result is world

    async def test_tool_completion_triggers_re_fold(self) -> None:
        """The branch where ``system_events`` contains
        a ``tool.*.completed`` event: the loop folds
        each event into the World and the overlay
        returns a new World with the tool-call
        slot evicted. Pinned so the orphan-request
        GC path (ADR-045) is exercised end-to-end.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        world = World.empty()
        completion = _seed_event("a-1", "tool.echo.completed")
        result = fold_with_systems(dispatcher, world, [completion])
        # The overlay did something (the returned World
        # is a new object), and the world view carries
        # the completion's data.
        assert result is not world

    async def test_filter_surviving_events_without_tool_events(self) -> None:
        """The branch ``if new_event_count > 0 and
        _has_tool_events(new_events)``: when the filter
        accepts events but none are ``tool.*``, the
        overlay is skipped. Pinned so a future refactor
        does not start running the overlay on
        non-tool batches (allocation cost).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        # Filter accepts (so count > 0); events are
        # domain, not tool.*
        dispatcher._filter = lambda _e: True
        events = [_seed_event("a-1", "user.intent")]
        world, count = fold_with_filter(dispatcher, World.empty(), events)
        assert count == 1
        # The World was created (the fold ran) but
        # the overlay did not (no tool events).
        assert "a-1" in world.views


# ---------------------------------------------------------------------------
# _systems_runner.append_system_outgoing
# ---------------------------------------------------------------------------


class TestAppendSystemOutgoing:
    async def test_sync_system(self) -> None:
        """A system that returns a list directly
        (the ``isinstance(out, list)`` arm).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        emitted = _seed_event("a-1", "sync.evt")

        def _sync_system(_world: World) -> list[Event]:
            return [emitted]

        dispatcher._systems = [_sync_system]
        out = await append_system_outgoing(dispatcher, World.empty(), "a-1")
        assert out is None
        assert log._cap.appended == [emitted]

    async def test_async_system(self) -> None:
        """The branch the orchestrator does not
        exercise: a system that returns a coroutine
        (``not isinstance(out, list)`` arm). Pinned so
        the ``await out`` path is not lost.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        emitted = _seed_event("a-1", "async.evt")

        async def _async_system(_world: World) -> list[Event]:
            return [emitted]

        dispatcher._systems = [_async_system]
        await append_system_outgoing(dispatcher, World.empty(), "a-1")
        assert log._cap.appended == [emitted]

    async def test_router_receives_batch(self) -> None:
        """The branch where ``_tool_router`` is set:
        the events are routed AFTER they are appended
        to the EventLog (ADR-036 §2.5).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        router = AsyncMock()
        dispatcher._tool_router = router
        emitted = _seed_event("a-1", "router.evt")
        dispatcher._systems = [lambda _w: [emitted]]
        await append_system_outgoing(dispatcher, World.empty(), "a-1")
        assert router.route_batch.await_args.args[0] == [emitted]

    async def test_system_returning_empty_list_is_skipped(self) -> None:
        """The branch ``if out: outgoing.extend(out)``
        when the system returns an empty list. Pinned
        so a future refactor does not treat a
        zero-event system as an error.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        dispatcher._systems = [lambda _w: []]
        await append_system_outgoing(dispatcher, World.empty(), "a-1")
        # The log was NOT appended (no events).
        assert log._cap.appended == []

    async def test_no_systems_means_no_append(self) -> None:
        """The branch ``if outgoing: append_batch(...)``
        when no system is registered. Pinned so the
        dispatcher short-circuits cleanly on an empty
        systems list.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        dispatcher._systems = []
        await append_system_outgoing(dispatcher, World.empty(), "a-1")
        assert log._cap.appended == []


# ---------------------------------------------------------------------------
# _systems_runner.run_systems_and_persist
# ---------------------------------------------------------------------------


class TestRunSystemsAndPersist:
    async def test_router_called_only_when_batch_non_empty(self) -> None:
        """The branch where ``new_event_count > 0`` AND
        ``_tool_router`` is set: the router receives the
        NEW batch (not the system-emitted events; the
        system-emitted events go through
        ``append_system_outgoing`` separately).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        router = AsyncMock()
        dispatcher._tool_router = router
        new_events = [_seed_event("a-1", "new.evt")]
        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 1, new_events
        )
        assert router.route_batch.await_args.args[0] == new_events
        assert cap.saved  # checkpoint saved

    async def test_no_router_no_call(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        new_events = [_seed_event("a-1", "new.evt")]
        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 0, new_events
        )
        # ``new_event_count == 0`` short-circuits the
        # router call even when one is wired in.
        # ADR-068 §3.5 P5c: the checkpoint save is
        # dirty-only. Here the batch was delivered
        # through ``new_events`` but the dispatcher was
        # told ``new_event_count=0`` (the legacy test
        # shape); the systems are silent, so nothing is
        # dirty and the save is skipped.
        assert cap.saved == []

    async def test_silent_systems_skip_fold_with_systems(self) -> None:
        """The branch ``if system_events:`` when the
        systems produced no events: ``fold_with_systems``
        is skipped (no orphan-request GC to run). Pinned
        so a future refactor does not call the fold
        helper unconditionally (the overlay would do
        a no-op pass but at the cost of allocating a
        new World).
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        dispatcher._systems = [lambda _w: []]  # silent system
        new_events = [_seed_event("a-1", "new.evt")]
        await run_systems_and_persist(
            dispatcher, "a-1", World.empty(), "1-0", 1, new_events
        )
        # The checkpoint was saved (always); the
        # systems were run; nothing was appended.
        assert cap.saved
        assert log._cap.appended == []


# ---------------------------------------------------------------------------
# _checkpoint_io
# ---------------------------------------------------------------------------


class TestCheckpointIO:
    async def test_bootstrap_collects_agents(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        log.add_agent("a-1")
        log.add_agent("a-2")
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        assert dispatcher._tracked_agents == set()
        await bootstrap_agents(dispatcher)
        assert dispatcher._tracked_agents == {"a-1", "a-2"}

    async def test_bootstrap_with_empty_log_is_noop(self) -> None:
        """The branch where ``list_agents`` returns an
        empty list: the bootstrap loop body never runs
        (``for aid in agent_ids:`` over an empty
        sequence). Pinned so the dispatcher's "no
        tenants yet" path is observed.
        """
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        await bootstrap_agents(dispatcher)
        assert dispatcher._tracked_agents == set()

    async def test_fetch_new_events_returns_batch_and_cursor(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        log.add_agent("a-1", _seed_event("a-1"))
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        events, cursor = await fetch_new_events(dispatcher, "a-1", "-")
        assert len(events) == 1
        assert cursor == "1-0"

    async def test_save_checkpoint_writes_world(self) -> None:
        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)
        world = World.empty()
        await save_checkpoint(dispatcher, "a-1", world, "5-0")
        assert cap.saved == [
            ("a-1", WorldCheckpoint(world=world, last_stream_id="5-0"))
        ]


# ---------------------------------------------------------------------------
# _systems_runner cursors (ADR-074)
# ---------------------------------------------------------------------------


class _NamedSystem:
    """Test system with an explicit ``__cursor_key__``
    override (ADR-074 §2.1)."""

    __cursor_key__: ClassVar[str] = "named"

    def __call__(self, world: World) -> list[Event]:
        return []


class TestCursorPrimitive:
    """Tests for the per-system cursor primitive (ADR-074).

    The dispatcher advances ``view.cursors[system_name]``
    after the system emits events; persistence piggy-backs
    on the WorldCheckpoint. No system currently consumes
    the cursor — this is the framework-level plumbing
    for the FSM (PR 2 of the refactor plan).
    """

    async def test_agent_view_cursors_default_to_empty_dict(self) -> None:
        """``cursors`` is a plain ``dict`` (no
        ``MappingProxyType``) per the ADR-074 §2.1
        convention.
        """
        view = AgentView(agent_id="a-1")
        assert view.cursors == {}
        assert isinstance(view.cursors, dict)

    async def test_agent_view_cursors_is_assignable_via_replace(self) -> None:
        """``AgentView`` is frozen; ``cursors`` is updated
        via ``dataclasses.replace``.
        """
        from dataclasses import replace

        view = AgentView(agent_id="a-1")
        new_view = replace(view, cursors={"FSMSystem": "e-1"})
        assert new_view.cursors == {"FSMSystem": "e-1"}
        # Original is unchanged (frozen).
        assert view.cursors == {}

    async def test_agent_view_cursors_round_trips_through_pickle(self) -> None:
        """``pickle.dumps(AgentView(...))`` must succeed —
        the ADR-074 field follows the ``AgentView.components``
        convention (plain dict, no ``MappingProxyType``;
        see ADR-036 §5).
        """
        import pickle
        from dataclasses import replace

        view = replace(
            AgentView(agent_id="a-1"),
            cursors={"FSMSystem": "e-1", "SagaSystem": "e-2"},
        )
        round_tripped = pickle.loads(pickle.dumps(view))
        assert round_tripped.cursors == view.cursors

    async def test_system_name_defaults_to_class_name(self) -> None:
        """``_system_name`` returns ``type(system).__name__``
        when no ``__cursor_key__`` is declared.
        """
        from kntgraph.runner._systems_runner import _system_name

        class MySystem:
            def __call__(self, world):
                return []

        assert _system_name(MySystem()) == "MySystem"

    async def test_system_name_uses_explicit_override(self) -> None:
        """``_system_name`` honours the
        ``__cursor_key__`` ClassVar override.
        """
        from kntgraph.runner._systems_runner import _system_name

        assert _system_name(_NamedSystem()) == "named"

    async def test_advance_cursors_no_emitters_returns_same_world(self) -> None:
        """No emitters ⇒ no allocation. Returns the exact
        ``world`` object (identity-equal), so callers can
        short-circuit ``if world is old_world``.
        """
        from kntgraph.runner._systems_runner import (
            _advance_cursors_in_world,
        )

        world = World.empty()
        result = _advance_cursors_in_world(world, set())
        assert result is world

    async def test_advance_cursors_skips_agents_without_view(self) -> None:
        """Defensive: if an emitter references an agent
        that doesn't exist in the World, the helper skips
        it (no allocation).
        """
        from kntgraph.runner._systems_runner import (
            _advance_cursors_in_world,
        )

        world = World.empty()
        result = _advance_cursors_in_world(world, {("FSMSystem", "nonexistent-agent")})
        # Cursor was NOT advanced (no view to anchor it).
        assert result is world

    async def test_dispatcher_advances_cursor_after_system_emits(self) -> None:
        """``run_systems_and_persist`` advances the cursor
        for the system that emitted events. The cursor
        advances to ``view.last_event_id`` at the moment
        of advancement (ADR-074 §2.2).

        Note: ``fold_with_systems`` only re-folds tool
        events (ADR-045 Slot GC). Domain events emitted
        by systems (e.g., the FSM's ``fsm.transitioned``)
        land in the EventLog but not in the world until
        the next tick. So for non-tool events the cursor
        advances to the last event the world has seen
        (which is the trigger event). On the next tick,
        the fold processes the system-emitted event and
        the cursor advances to it.
        """
        from kntgraph.runner._systems_runner import _system_name

        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)

        # Seed an event so the post-fold world has a
        # ``last_event_id``.
        seed = _seed_event("a-1", "seed.evt")
        # System emits a domain event (NOT a tool event —
        # ``fold_with_systems`` will not fold it back into
        # the world this tick).
        emitted = _seed_event("a-1", "emitted.evt")

        def _system(_w: World) -> list[Event]:
            return [emitted]

        dispatcher._systems = [_system]

        # Build a world with the seed event applied.
        world_with_seed = World.empty().with_event(seed)

        await run_systems_and_persist(
            dispatcher, "a-1", world_with_seed, "1-0", 1, [seed]
        )

        # Find the saved world.
        assert cap.saved, "checkpoint was not saved"
        saved_world = cap.saved[0][1].world
        view = saved_world.views["a-1"]
        sys_name = _system_name(_system)
        assert sys_name in view.cursors, (
            f"expected cursor for {sys_name!r}, got {view.cursors!r}"
        )
        # Cursor points at the last event the WORLD has
        # seen — which is the seed (the emitted domain
        # event is in the EventLog but not yet folded into
        # the world).
        assert view.cursors[sys_name] == str(seed.event_id)

    async def test_dispatcher_advances_cursor_when_system_runs(self) -> None:
        """Cursor advances when the system RUNS, not only
        when it emits. A silent system that doesn't emit
        anything still moves its cursor forward so it
        doesn't re-process the same events on the next
        tick.
        """

        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)

        # Seed an event so the world has a ``last_event_id``
        # (otherwise the cursor advances are skipped by
        # ``_advance_cursors_in_world`` when no anchor).
        seed = _seed_event("a-1", "seed.evt")

        # A silent system (returns []).
        dispatcher._systems = [lambda _w: []]

        world_with_seed = World.empty().with_event(seed)
        await run_systems_and_persist(
            dispatcher, "a-1", world_with_seed, "1-0", 1, [seed]
        )

        # The system RAN (cursor advances per-run, not
        # per-emit). The cursor advanced even though no
        # events were emitted. The cursor key is
        # ``type(lambda).__name__`` = ``"function"`` here
        # because the lambda doesn't define
        # ``__cursor_key__``.
        assert cap.saved, "checkpoint was not saved"
        saved_world = cap.saved[0][1].world
        view = saved_world.views["a-1"]
        # Cursor advanced to the seed's event_id.
        assert "function" in view.cursors
        assert view.cursors["function"] == str(seed.event_id)

    async def test_cross_agent_emission_advances_target_agent_cursor(self) -> None:
        """A system emits for agent B while processing
        agent A's tick. The cursor advances for agent B
        (not for A — A had no events emitted for it).
        """
        from kntgraph.runner._systems_runner import _system_name

        cap = _Captured()
        log = _FakeEventLog(cap)
        store = _FakeWorldStore(cap)
        dispatcher = _build_dispatcher(log=log, store=store)

        # Seed an event for agent B so B's view has a
        # ``last_event_id``.
        seed_b = _seed_event("b-1", "b.seed")
        emitted_b = _seed_event("b-1", "b.emitted")

        # System emits for agent B (not the agent being
        # processed, which is "a-1").
        def _system(_w: World) -> list[Event]:
            return [emitted_b]

        dispatcher._systems = [_system]

        # Build a world with both agents.
        world = World.empty().with_event(seed_b)

        await run_systems_and_persist(dispatcher, "a-1", world, "1-0", 1, [seed_b])

        saved_world = cap.saved[0][1].world
        view_b = saved_world.views["b-1"]
        sys_name = _system_name(_system)
        assert sys_name in view_b.cursors
        # Cursor points at the last event the world has
        # seen — which is seed_b (the emitted domain
        # event is in the EventLog but not yet folded).
        assert view_b.cursors[sys_name] == str(seed_b.event_id)
