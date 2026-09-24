# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Chaos tests for the dispatcher's crash-safety contract.

The audit (initial FSM/Sagas review) flagged this gap:
when the dispatcher crashes mid-rollback, a fresh
dispatcher built from the same Redis state must
reconstruct the saga's compensating state from the
EventLog + WorldCheckpoint (the source of truth + the
cached projection).

These tests pin two contracts:

  - **State preservation across restart**: a fresh
    dispatcher built from the same Redis state sees
    the same world (including ``SagaProgressComponent``
    in compensating state) by loading the WorldCheckpoint.

  - **EventLog re-derivation**: after a crash, the
    projection (re-fold) reconstructs the same state from
    the EventLog alone (the WorldCheckpoint can be lost).

Each test follows the Arrange / Act / Assert convention.
The **system under test** is identified at the top of
each test (``sut``) -- the dispatcher / projection /
checkpoint object whose contract is being pinned.

The "crash" is simulated by releasing the dispatcher
object and building a fresh one. No process kill is
involved; the property under test is "Redis state is
durable; the dispatcher is a cache".
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import pytest

from kntgraph.concordos.saga import (
    SagaConfig,
    SagaProjection,
    SagaStepConfig,
)
from kntgraph.concordos.saga._components import SagaProgressComponent
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.storage import ArchetypeStorage
from kntgraph.core.world import World
from kntgraph.core.world.projection import project_default
from kntgraph.core.world.view import AgentView
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._world_checkpoint._redis import (
    RedisWorldCheckpointStorage,
)
from kntgraph.infra.world_checkpoint import (
    IncrementalWorldStore,
    WorldCheckpoint,
)
from kntgraph.stream.event_log import EventLog


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> CorrelationContext:
    """Build a fresh ``CorrelationContext`` with a random flow id."""
    return CorrelationContext.new(correlation_id=uuid4())


def _compensating_world(
    agent_id: str,
    *,
    compensate_stack: list[str],
    step_states: dict[str, str],
) -> World:
    """Build a World whose view carries a
    ``SagaProgressComponent`` in compensating state.

    Used to seed the WorldCheckpoint with the state
    the dispatcher would have if a saga had just entered
    compensation. The dispatcher doesn't need to drive the
    saga to this state itself -- the chaos test verifies
    the dispatcher's restart path preserves whatever
    state is in the checkpoint + EventLog.
    """
    progress = SagaProgressComponent(
        saga_name="chaos",
        saga_id="saga-chaos",
        step_order=tuple(["step_a", "step_b", "step_c"]),
        current_step=compensate_stack[0] if compensate_stack else "",
        direction="compensating",
        step_states=MappingProxyType(dict(step_states)),
        step_results=MappingProxyType({}),
        compensate_stack=tuple(compensate_stack),
        started_at=datetime.now(tz=timezone.utc),
    )
    view = AgentView(
        agent_id=agent_id,
        components={SagaProgressComponent: progress},
    )
    return World(
        tick=0,
        storage=ArchetypeStorage(),
        views={agent_id: view},
    )


def _progress_snapshot(view: AgentView) -> tuple[str, list[str], dict[str, str]]:
    """Snapshot the saga's compensating state into a
    tuple that's cheap to compare across dispatcher
    instances.
    """
    progress = view.get_component(SagaProgressComponent)
    if progress is None:
        return ("absent", [], {})
    return (
        progress.direction,
        list(progress.compensate_stack),
        dict(progress.step_states),
    )


# ---------------------------------------------------------------------------
# Chaos tests
# ---------------------------------------------------------------------------


async def test_dispatcher_restart_preserves_compensating_world_state(
    clean_redis: Any,
) -> None:
    """The dispatcher's WorldCheckpoint is durable across
    a simulated crash: after the in-memory dispatcher is
    released, a fresh dispatcher built from the same
    Redis state loads the same compensating state.

    **System under test**: ``IncrementalWorldStore``
    (the Redis-backed checkpoint cache).

    Arrange:
        - Build a World whose view carries a saga in
          compensating state (step_a on the
          compensate_stack).
        - Save the WorldCheckpoint to Redis via
          ``IncrementalWorldStore.save``.

    Act:
        - Build dispatcher 1, hold a reference, release
          it (simulating the crash).
        - Build dispatcher 2 from the same Redis state.
        - Load the WorldCheckpoint via the shared store.

    Assert:
        - Dispatcher 2's loaded world has the same
          compensating state as the seeded one
          (direction, compensate_stack, step_states).
    """
    agent_id = "a-chaos-1"

    # Arrange.
    world = _compensating_world(
        agent_id,
        compensate_stack=["step_a"],
        step_states={
            "step_a": "completed",
            "step_b": "failed",
        },
    )
    expected_snapshot = (
        "compensating",
        ["step_a"],
        {"step_a": "completed", "step_b": "failed"},
    )
    log = EventLog(RedisEventLogAdapter(clean_redis))
    sut = IncrementalWorldStore(RedisWorldCheckpointStorage(clean_redis))
    # Simulate dispatcher 1 having run a tick and saved.
    await sut.save(
        agent_id,
        WorldCheckpoint(world=world, last_stream_id="-"),
    )

    # Act.
    del sut  # "crash" -- release the in-memory state.
    sut_after_restart = IncrementalWorldStore(RedisWorldCheckpointStorage(clean_redis))
    ckpt = await sut_after_restart.load(agent_id)
    view = ckpt.world.get_agent(agent_id)
    assert view is not None, (
        f"loaded world should have a view for {agent_id!r}; "
        f"world.views keys: {list(ckpt.world.views.keys())!r}"
    )
    loaded_snapshot = _progress_snapshot(view)

    # Assert.
    assert loaded_snapshot == expected_snapshot, (
        f"WorldCheckpoint round-trip mismatch:\n"
        f"  expected: {expected_snapshot!r}\n"
        f"  loaded:   {loaded_snapshot!r}"
    )
    # The EventLog is still empty for this agent (the
    # test didn't append anything); the WorldCheckpoint
    # is the only durable state in this scenario.
    assert await log.read(agent_id) == []


async def test_saga_projection_replays_event_log_into_world_after_wipe(
    clean_redis: Any,
) -> None:
    """After the WorldCheckpoint is wiped (the worst-case
    crash scenario: every cached projection is lost), the
    ``SagaProjection`` must reconstruct the compensating
    state from the EventLog alone.

    This is the strongest crash-safety property: the
    EventLog is the source of truth; the WorldCheckpoint
    is a cache.

    **System under test**: ``SagaProjection`` (the pure
    fold from events to ``SagaProgressComponent``).

    Arrange:
        - Append the saga-lifecycle events that drive a
          saga forward into the compensation phase
          (``started`` + ``compensating`` + per-step
          ``compensation_started``). The
          ``compensation_started`` event alone is
          insufficient (the projection requires
          ``saga_id`` to be set by ``started`` first); the
          test simulates the real event sequence the
          dispatcher would have emitted before the crash.

    Act:
        - Build a fresh base view (no checkpoint
          available; equivalent to a brand-new dispatcher
          after a crash that wiped the cache).
        - Run ``SagaProjection`` on the events.

    Assert:
        - The resulting ``AgentView`` has a
          ``SagaProgressComponent`` in ``compensating``
          state with ``step_a`` on the
          ``compensate_stack``.
    """
    agent_id = "a-chaos-2"

    # Arrange.
    log = EventLog(RedisEventLogAdapter(clean_redis))
    sut = SagaProjection(
        SagaConfig(
            name="chaos",
            saga_timeout_ms=60_000,
            fail_when=None,
            steps=(
                SagaStepConfig(
                    name="step_a",
                    tool_name="tool_a",
                    compensate_tool="undo_a",
                ),
            ),
        )
    )
    # The minimal event sequence the projection needs to
    # materialise a compensating saga: ``started`` sets
    # ``saga_id``; ``compensating`` flips the direction;
    # ``compensation_started`` adds the step to the
    # compensate_stack.
    events = [
        Event.create(
            event_type="saga.chaos.started",
            agent_id=agent_id,
            event_class="domain",
            data={"saga_id": "saga-1"},
            correlation=_ctx(),
        ),
        Event.create(
            event_type="saga.chaos.compensating",
            agent_id=agent_id,
            event_class="domain",
            data={"reason": "step_failure", "saga_id": "saga-1"},
            correlation=_ctx(),
        ),
        Event.create(
            event_type="saga.chaos.step_a.compensation_started",
            agent_id=agent_id,
            event_class="domain",
            data={"step_name": "step_a", "saga_id": "saga-1"},
            correlation=_ctx(),
        ),
    ]
    # Round-trip through Redis so we exercise the real
    # event-log codec (the projection's input is the
    # ``Event`` object; the EventLog is the persistence
    # boundary).
    for e in events:
        await log.append(e)
    persisted_events = await log.read(agent_id)
    assert len(persisted_events) == 3

    # Act.
    base_views = project_default(persisted_events)
    world = World(
        tick=1,
        storage=ArchetypeStorage(),
        views=base_views,
    )
    projected_world = sut(world, persisted_events)
    view = projected_world.views[agent_id]
    progress = view.get_component(SagaProgressComponent)

    # Assert.
    assert progress is not None, (
        "SagaProjection should materialise SagaProgressComponent "
        "from a started + compensating + compensation_started "
        "event sequence (the minimal chain to recover "
        "compensating state from the EventLog alone)"
    )
    assert progress.direction == "compensating"
    assert "step_a" in progress.compensate_stack, (
        f"step_a should be on the compensate_stack after replay; "
        f"got {progress.compensate_stack!r}"
    )
    assert progress.step_states.get("step_a") == "compensating_started"
    assert progress.saga_id == "saga-1", (
        f"saga_id should be recovered from the started event; got {progress.saga_id!r}"
    )
