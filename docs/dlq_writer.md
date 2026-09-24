<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# DLQ writer — bridge from `*.dlq` events to the DLQ storage

The saga emits typed `*.dlq` events (e.g. `saga.<saga_name>.dlq`) to keep
itself side-effect-free (ADR-069 §5.2). This document describes the
**adapter** that closes the loop: it reads `*.dlq` events from the
outgoing batch and pushes a `DeadLetterEvent` to the `DeadLetterQueue`
storage.

> **Status**: implemented. The writer lives at
> `src/kntgraph/runner/_dlq_writer.py` and is invoked from
> `_systems_runner.append_system_outgoing` after the EventLog
> append. Idempotency comes for free from the DLQ storage.

---

## 1. The contract

The saga's [`_records.dlq_event`][dlq-evt] emits a single typed event
per compensation failure:

```python
emit(
    saga,
    trigger,
    event_type=f"saga.{saga._cfg.name}.dlq",
    data={
        "saga_id": progress.saga_id,
        "stuck_step": progress.current_step,
        "step_states": dict(progress.step_states),
    },
)
```

The `_records.dlq_event` docstring captures the intent:

> The actual DLQ insertion is performed by an adapter system that
> reads this event and appends to ``knt:dlq:saga:<name>``; the saga
> system only emits the typed event so the DLQ adapter stays out of
> the saga's dependency graph.

The DLQ writer in this module is that adapter.

[dlq-evt]: ../src/kntgraph/concordos/saga/_records.py

---

## 2. Why a separate module

Two reasons:

1. **Separation of concerns.** The saga stays pure: it reads the
   `World`, decides what `*.dlq` event to emit, and emits it. The
   writer is the I/O side: it talks to Redis (via the DLQ storage).
   Co-locating them would re-couple the saga to Redis, undoing
   ADR-069 §5.2.

2. **Optionality.** The writer is only useful when a `DeadLetterQueue`
   is wired. Operators that don't care about DLQ still get the saga's
   typed event in the EventLog (the audit trail) — they just don't
   get the persisted DLQ entry. The dispatcher passes `dlq=None`
   in this case; the writer short-circuits to `[]`.

---

## 3. The pipeline

```
saga system           begin_compensation       events/dlq/store.py
   │                       │                          │
   │                       │  emit saga.X.dlq          │
   │                       ▼                          │
   │              ┌─────────────────────┐             │
   │              │  EventLog (Redis)   │             │
   │              └─────────────────────┘             │
   │                       │                          │
   ▼                       ▼                          │
append_system_outgoing ──► writer.append_dlq_events ──► DLQ storage
                          (here)                       (Redis stream +
                                                        per-event id index)
```

Three hooks:

1. The saga emits `saga.X.dlq` (in `_compensation.py:begin_compensation`).
2. The dispatcher's `_systems_runner.append_system_outgoing` calls
   the writer right after the EventLog append (durability ordering
   preserved — see [§4](#4-durability-ordering)).
3. The writer builds a `DeadLetterEvent` from the saga's event and
   calls `dlq.append(...)` for each.

---

## 4. Durability ordering

The writer is invoked **after** the dispatcher's outgoing batch is
appended to the EventLog:

```python
# _systems_runner.append_system_outgoing, abridged:
if outgoing:
    await dispatcher._log.append_batch(outgoing)
    if dispatcher._tool_router is not None:
        await dispatcher._tool_router.route_batch(outgoing)
    sink = getattr(dispatcher, "_metrics_sink", None)
    if sink is not None:
        for event in outgoing:
            if event.event_type.endswith(".compensation_started"):
                sink.incr_compensation_started()
    from ._dlq_writer import append_dlq_events
    await append_dlq_events(
        outgoing, getattr(dispatcher, "_dlq", None)
    )
```

Order matters: if the writer were called **before** the EventLog
append, a crash between the two would leave the DLQ with an entry
that the audit trail doesn't reflect (no saga event to explain why
the entry exists). Conversely, if the EventLog append succeeded but
the DLQ write failed, the `*.dlq` event is durable and a future
replay retries the append (the storage is idempotent on
`<event_id>:<reason>` — see [§5](#5-idempotency)).

---

## 5. Idempotency

`DeadLetterQueue.append()` is already idempotent on `<event_id>:<reason>`:

```python
# infra/redis/_dlq.py: storage checks the per-event index BEFORE
# the XADD; if the key exists, it returns ``Ok(PLACEHOLDER)`` without
# creating a duplicate.
existing = await self.client.hget(DLQ_EVENT_INDEX, idem_key)
if existing is not None:
    return Ok(existing.decode(...))
```

The writer inherits this property:

- **Dispatcher restart**: if the process crashes between
  `EventLog.append_batch(...)` and `append_dlq_events(...)`, the
  `*.dlq` event is durable but the DLQ entry is not. The next tick
  re-folds the event from the EventLog; the writer's dedup is
  hit-or-miss — if a prior run did write to the DLQ, the dedup
  catches the second write; if not, the second write creates the
  entry.

- **Repeat tick in the same run**: the writer's pre-check via
  `dlq.get_event(event_id)` filters out entries already in the
  DLQ (see `tests/unit/runner/test_dlq_writer.py`). So even if the
  same batch passes through twice (e.g. on a re-fold), the second
  write is filtered out.

---

## 6. API

### Public

```python
async def append_dlq_events(
    outgoing: list[Event],
    dlq: Optional[DeadLetterQueue],
    *,
    default_reason: DLQReason = DEFAULT_DLQ_REASON,
) -> list[str]:
    """Push every ``*.dlq`` event in ``outgoing`` to the DLQ.

    Iterates ``outgoing`` in order. For each event whose
    ``event_type`` ends with :data:`DLQ_EVENT_SUFFIX`, builds a
    :class:`DeadLetterEvent` and calls
    :meth:`DeadLetterQueue.append`. Storage errors are logged
    but do NOT abort the loop -- one failed event does not
    block the others.

    Args:
        outgoing: the events the dispatcher's tick loop
            just appended to the EventLog.
        dlq: the :class:`DeadLetterQueue` instance the
            dispatcher was constructed with (``None`` when
            the application did not opt in to a DLQ).
            When ``None`` the function is a no-op and returns
            ``[]``.
        default_reason: :class:`DLQReason` assigned when
            the event's ``data`` payload has no ``reason``
            key. Operators may pass a different default for
            verticals with a richer failure vocabulary.

    Returns:
        List of stream ids assigned by the storage, one
        per **fresh** entry. Idempotent re-runs (dedup hit)
        are pre-checked and skipped -- the returned list
        accurately reflects "what was newly written".
    """
```

### Constants

| Name | Value | Meaning |
| --- | --- | --- |
| `DLQ_EVENT_SUFFIX` | `".dlq"` | Suffix that marks an event as DLQ-bound. The saga emits `saga.<name>.dlq`; future event types sharing the same shape (e.g. nested-saga compensation failure) just extend the suffix. |
| `DEFAULT_DLQ_REASON` | `DLQReason.PROCESSING_FAILED` | Reason assigned when the `*.dlq` event has no `reason` key in its data payload. The saga's events fall in this bucket. Override via the `default_reason=` parameter for verticals with a richer failure vocabulary (e.g. `DLQReason.TIMEOUT` for explicit timeout-tagged failures). |
| `PLACEHOLDER` | `"PLACEHOLDER"` | Sentinel returned by `DeadLetterQueue.append` when the dedup boundary is hit. The writer filters this out of the returned `stream_ids` list (the caller doesn't need to distinguish "wrote" from "already there" for accounting). |

---

## 7. Failure modes

| Scenario | Behavior |
| --- | --- |
| `dlq=None` (operator opted out) | No-op, returns `[]`. The saga stays side-effect-free; the `*.dlq` event is still in the EventLog as an audit trail. |
| `dlq.append` returns `Err(...)` | Logged via `structlog` (`dlq_writer.append_failed`); loop continues with the next event. **No fail-fast.** |
| `dlq.append` returns `Ok(PLACEHOLDER)` | Idempotency hit; not added to the returned `stream_ids` list. |
| Storage transient error on event `N` | Logged, the loop continues; events `N+1...` still get their DLQ entry. |
| All storage calls fail | The writer returns `[]` but the EventLog still has the `*.dlq` events; a future replay retries. |

---

## 8. Operational notes

- **Best-effort, not transactional.** The writer does not participate
  in a Redis MULTI/EXEC. A storage error between the EventLog append
  and the DLQ append leaves the EventLog with the `*.dlq` event
  but the DLQ without it. The TTL sweeper eventually marks the
  request as stale; the saga's `compensation_failed` event is
  emitted on the next tick. The saga stays alive.

- **Pre-check is best-effort.** The writer calls
  `dlq.get_event(event_id)` before each append to filter dedup
  hits. This is an optimization, not a correctness guarantee: if
  the pre-check misses a duplicate (e.g. the DLQ entry is written
  by a parallel process between the pre-check and the append),
  the storage's own idempotency boundary catches it.

- **Mutating operations are best-effort.** Storage errors are logged
  and the loop continues; a crash mid-loop leaves the EventLog
  consistent (every emitted event is there) but the DLQ may be
  partially written. The TTL sweeper's recovery path + the
  storage's idempotency keep the eventual state consistent.

---

## 9. References

- `src/kntgraph/runner/_dlq_writer.py` — the writer itself.
- `src/kntgraph/events/dlq/store.py` — `DeadLetterQueue`
  (storage + idempotency boundary).
- `src/kntgraph/concordos/saga/_compensation.py` — `begin_compensation`
  (emits `*.dlq` events).
- `src/kntgraph/runner/_systems_runner.py` — `append_system_outgoing`
  (the hook point).
- `tests/unit/runner/test_dlq_writer.py` — unit tests (fake DLQ).
- `tests/integration/test_dlq_writer_e2e.py` — end-to-end test
  through the dispatcher against real Redis (`clean_redis`).
- ADR-069 §5.2 — the "saga stays side-effect-free" invariant.
- ADR-075 §2.3 — Tier 4 observability surface (the `*.dlq`
  events are a leading indicator of saga trouble).
- [docs/dead_letter_queue.md](dead_letter_queue.md) — the DLQ
  **storage** API (separate from the writer).
