<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Chaos testing — crash-recovery contracts for the dispatcher

The audit (initial FSM/Sagas review) flagged a gap: when the
dispatcher crashes mid-rollback, a fresh dispatcher built from the
same Redis state must reconstruct the saga's compensating state
from the EventLog + WorldCheckpoint (the source of truth + the
cached projection) and continue the compensation.

This document describes the **chaos testing pattern** that pins
that contract.

> **Status**: implemented. Two tests in
> `tests/integration/test_dispatcher_chaos.py` cover the
> patterns described below; new chaos tests follow the
> same template.

---

## 1. The pattern

```
┌─────────────────────────────────────────────────────────┐
│              Chaos Test Pattern                          │
├─────────────────────────────────────────────────────────┤
│  1. Arrange  Build sut from real Redis (no mocks).        │
│            Manually seed the durable state (WorldCheckpoint│
│            or EventLog) the production code would have built│
│            after some ticks.                              │
│  2. Act      Release the in-memory dispatcher (simulates  │
│            the process crash). Build a fresh dispatcher │
│            with the same Redis state.                     │
│  3. Assert   The fresh dispatcher loads the same state   │
│            from Redis (checkpoint or fold). When the     │
│            scenario includes a late-arriving event, drive │
│            the fresh dispatcher and verify the saga       │
│            advances correctly.                            │
└─────────────────────────────────────────────────────────┘
```

The "crash" is simulated by releasing the dispatcher object and
building a fresh one. **No process kill is involved.** The
property under test is "Redis state is durable; the dispatcher
is a cache."

---

## 2. Test template (Arrange / Act / Assert)

The pattern follows the project convention (`# Arrange:` / `# Act:`
/ `# Assert:` comments inside the test body):

```python
async def test_<sut>_recovers_<scenario>(
    clean_redis: Any,
) -> None:
    """One-sentence summary of the contract being pinned.

    **System under test**: <class name> -- the component
    whose crash-recovery contract is the focus.

    Arrange:
        - Set up the durable state the production code would
          have built at this point in the scenario.

    Act:
        - Release the in-memory dispatcher.
        - Build a fresh dispatcher with the same Redis state.
        - Drive a tick (if the scenario includes a late event).

    Assert:
        - The fresh dispatcher sees the expected state.
    """
    agent_id = "<fixture-id>"

    # Arrange.
    sut = <build the system under test from clean_redis>

    # Act.
    del sut  # "crash" -- release the in-memory state.
    sut_after_restart = <build a fresh sut from clean_redis>
    result = <drive a tick / load the checkpoint>

    # Assert.
    assert result == <expected>
```

The "**System under test**" line names the **sut** variable —
the object whose crash-recovery contract the test pins. The
collection of tests reads as a behavioural spec for that object.

---

## 3. Worked example — WorldCheckpoint durability

The simplest chaos test pins the durability of the
`IncrementalWorldStore` (the cache of the post-fold World +
the last cursor). The pattern lives in
`tests/integration/test_dispatcher_chaos.py`:

```python
async def test_dispatcher_restart_preserves_compensating_world_state(
    clean_redis: Any,
) -> None:
    """The dispatcher's WorldCheckpoint is durable across
    a simulated crash.

    **System under test**: ``IncrementalWorldStore``
    (the Redis-backed checkpoint cache).
    """
    agent_id = "a-chaos-1"

    # Arrange.
    world = _compensating_world(
        agent_id,
        compensate_stack=["step_a"],
        step_states={"step_a": "completed", "step_b": "failed"},
    )
    expected_snapshot = (
        "compensating",
        ["step_a"],
        {"step_a": "completed", "step_b": "failed"},
    )
    sut = IncrementalWorldStore(RedisWorldCheckpointStorage(clean_redis))
    await sut.save(
        agent_id, WorldCheckpoint(world=world, last_stream_id="-")
    )

    # Act.
    del sut  # "crash"
    sut_after_restart = IncrementalWorldStore(
        RedisWorldCheckpointStorage(clean_redis)
    )
    ckpt = await sut_after_restart.load(agent_id)
    view = ckpt.world.get_agent(agent_id)
    loaded_snapshot = _progress_snapshot(view)

    # Assert.
    assert loaded_snapshot == expected_snapshot
```

The "crash" is the `del sut` line; the "restart" is the new
`IncrementalWorldStore` constructed with the same Redis. The
fresh dispatcher reads the same `knt:<agent>:world` key from
Redis and sees the same compensating state.

---

## 4. Worked example — EventLog re-derivation

The strongest crash-safety property: the EventLog alone is the
source of truth. Even if the WorldCheckpoint is wiped, the
projection must reconstruct the same state from the EventLog.

```python
async def test_saga_projection_replays_event_log_into_world_after_wipe(
    clean_redis: Any,
) -> None:
    """After the WorldCheckpoint is wiped, the
    ``SagaProjection`` must reconstruct the compensating
    state from the EventLog alone.

    **System under test**: ``SagaProjection`` (the pure fold).
    """
    agent_id = "a-chaos-2"

    # Arrange.
    log = EventLog(RedisEventLogAdapter(clean_redis))
    sut = SagaProjection(SagaConfig(name="chaos", steps=(...)))

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
    for e in events:
        await log.append(e)
    persisted_events = await log.read(agent_id)

    # Act.
    base_views = project_default(persisted_events)
    world = World(tick=1, storage=ArchetypeStorage(), views=base_views)
    projected_world = sut(world, persisted_events)
    view = projected_world.views[agent_id]
    progress = view.get_component(SagaProgressComponent)

    # Assert.
    assert progress.direction == "compensating"
    assert "step_a" in progress.compensate_stack
```

The ``sut(world, events)`` invocation is the production fold. The
fresh ``sut`` instance has no cached state; the test proves that the
**EventLog alone** is sufficient to reconstruct the compensating
state. This is the contract that lets the dispatcher's checkpoint
be a cache (wipe-able, regenerable) rather than a source of truth.

---

## 5. Anti-patterns

- ❌ Mocking the dispatcher. The "system under test" must be the
  production code path — mocking defeats the purpose (you're not
  testing crash recovery, you're testing your mock).

- ❌ Using `fakeredis` for chaos tests. The crash-safety
  contract is about **Redis state durability**; `fakeredis`
  shares memory between dispatcher instances and is not
  representative of a real restart.

- ❌ Asserting only on the side effects. A chaos test that
  passes when the dispatcher crashes but `payload == payload`
  by accident isn't catching anything. Assert on the
  **state surface** (the checkpoint contents, the projection's
  output, the dispatched events) — the same way the production
  code reads them.

- ❌ Skipping the "**System under test**" docstring line. The
  name of the `sut` variable is the spec's anchor; without it,
  the test reads as a generic chaos test instead of a contract
  pin.

---

## 6. When to add a new chaos test

Add a chaos test when:

1. The audit (or a code review) flags a "what if the process
   crashes here?" question. The test pins the answer.
2. You change the EventLog schema, the checkpoint format, or
   the projection's read path. Existing chaos tests fail if
   the crash-recovery contract breaks — they are regression
   tests for the contract.
3. You add a new subscriber to the dispatcher's tick loop
   (e.g. a new metrics sink, a new side-effect emitter).
   Existing chaos tests confirm the new code doesn't
   re-introduce a "discarded in-memory state" assumption.

---

## 7. References

- `tests/integration/test_dispatcher_chaos.py` — the two
  worked examples above (`IncrementalWorldStore` durability +
  `SagaProjection` re-derivation).
- `src/kntgraph/runner/reactive.py:ReactiveDispatcher` — the
  production dispatcher.
- `src/kntgraph/infra/world_checkpoint.py:IncrementalWorldStore` —
  the checkpoint cache.
- `src/kntgraph/concordos/saga/_state.py:SagaProjection` — the
  projection under test.
- ADR-069 §11.18.2 — granular `compensation_started` markers
  that the projection reads.
- ADR-075 §2.3 — Tier 4 observability (different concern, but
  the same dispatcher object; the audit group reviewed both).
- [docs/metrics_sink.md](metrics_sink.md) — observability
  counterpart (the metrics sink is also wired in the
  dispatcher's tick loop and could break the contract).
- [docs/dlq_writer.md](dlq_writer.md) — the DLQ writer is the
  sibling of the metrics sink in the tick loop; it shares the
  same crash-safety story.
