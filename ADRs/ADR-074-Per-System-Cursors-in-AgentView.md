<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-074: Per-System Cursors in AgentView

- **Status:** Accepted
- **Date:** 2026-09-12
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-018](./ADR-018-WorldIncremental-WorldSystem.md) — WorldSystem + ReactiveDispatcher (defines AgentView)
  - [ADR-069](./ADR-069-Agent-Concordo-Foundation.md) — Concordo Foundation (cross-cutting concerns)
  - [ADR-071](./ADR-071-BusinessFSM-Concordo.md) — BusinessFSM Concordo (consumer)
  - [ADR-072](./ADR-072-WorkflowSaga-Concordo.md) — WorkflowSaga Concordo (potential future consumer)

---

## 1. Context

The reactive dispatcher (ADR-018) iterates systems
per agent per tick. Each system reads the post-fold
`AgentView` and emits events based on what it sees.

Two related problems arose when designing the
BusinessFSM (ADR-071 §3.4.2):

1. **Single-slot projection.** `view.domain_phase`
   and `view.last_event_id` carry only the **most
   recent** event. If an agent produces multiple
   domain events in one tick (an external adapter
   emits both `invoice.created` and
   `invoice.validated` for the same agent in the
   same fold), only the last is visible to systems
   reading the view. Earlier events are silently
   dropped from the trigger surface.

2. **Per-system cursor.** A system that needs to
   detect which events it has already processed
   (for delta-scan across multi-event ticks, or
   for replay-safety after process restart) needs
   to track this somewhere. Storing it as internal
   state on the system instance loses it on
   restart; persisting it externally (Redis) adds
   a new storage layer.

This ADR captures the framework-level primitive
that addresses both: a **per-system, per-agent cursor**
stored **in the AgentView itself**, managed by the
dispatcher, persisted via the existing
WorldCheckpoint.

---

## 2. Decision

`AgentView` (defined in `core/world/view.py`) gains
one new field:

```python
@dataclass(frozen=True, slots=True)
class AgentView:
    # ... existing fields (last_event_id, domain_phase,
    # operational_phase, last_event_principal_id, etc.) ...

    # NEW: per-system cursor. Maps the system's name
    # to the event_id of the most recent event the
    # system has processed for this agent.
    #
    # Absent key = the system has never processed an
    # event for this agent (full re-derivation from the
    # EventLog is required).
    #
    # Advanced by the dispatcher in
    # ``runner/_systems_runner.py`` after the system
    # emits events; persisted via WorldCheckpoint (the
    # checkpoint already pickles the AgentView).
    #
    # Type hint is ``Mapping`` but the runtime value is a
    # plain ``dict`` (matches the existing convention of
    # ``AgentView.components``: see ADR-036 §5 which
    # explicitly removed ``MappingProxyType`` from
    # AgentView fields because it was redundant with
    # ``frozen=True`` and broke World pickling). The
    # frozen dataclass prevents reassignment; mutation
    # of the dict's contents is a discipline enforced
    # by code review, not runtime.
    cursors: Mapping[str, str] = field(default_factory=dict)
```

### 2.1 Semantics

- **Key**: the system's name. By default,
  `type(system).__name__`. Systems can override via
  a class variable:

  ```python
  class BusinessFSMConcordo:
      class FSMSystem(WorldSystem):
          __fsm_system_name__: ClassVar[str] = "fsm"
  ```

  The override exists so subclasses with the same
  class name in different modules don't collide.

- **Value**: the `event_id` (UUID string) of the most
  recent event the system has processed for this
  agent.

- **Absent key**: the system has never processed an
  event for this agent. The system treats this the
  same as `cursor_id == None` and re-derives from the
  start of the EventLog.

### 2.2 Dispatcher advances the cursor

The dispatcher advances the cursor **after** the
system runs and **only if** the system emitted events
for the agent:

```python
# runner/_systems_runner.py (modified)
async def append_system_outgoing(
    dispatcher, world, agent_id, *, return_events=False
) -> list[Event] | None:
    outgoing: list[Event] = []
    with correlation_middleware.scope():
        for system in dispatcher._systems:
            out = system(world)
            if not isinstance(out, list):
                out = await out
            if out:
                outgoing.extend(out)
    if outgoing:
        # NEW: advance per-system cursor for systems that
        # emitted events for this agent.
        view = world.views[agent_id]
        new_cursors = dict(view.cursors)
        for system in dispatcher._systems:
            if any(e.agent_id == agent_id for e in outgoing):
                new_cursors[_system_name(system)] = view.last_event_id
        # Re-fold the view with the new cursors. Frozen
        # dataclass means we rebuild the view object via
        # ``replace`` rather than mutate. ``new_cursors``
        # is a plain ``dict`` (no ``MappingProxyType`` —
        # see ADR-036 §5 for the rationale on AgentView
        # fields).
        new_view = replace(view, cursors=new_cursors)
        # ... update world.views[agent_id] = new_view ...
        await dispatcher._log.append_batch(outgoing)
        if dispatcher._tool_router is not None:
            await dispatcher._tool_router.route_batch(outgoing)
    if return_events:
        return outgoing
    return None


def _system_name(system: WorldSystem) -> str:
    """Resolve the cursor key for a system instance."""
    return getattr(system, "__fsm_system_name__", type(system).__name__)
```

The cursor advances to `view.last_event_id` (the most
recent event the **view** has seen), not to a specific
emitted event's id. The system reads the OLD cursor
during its `__call__`; the dispatcher writes the NEW
cursor after the call returns.

### 2.3 Persistence

WorldCheckpoint pickles the AgentView. Adding
`cursors` to AgentView means the cursor is persisted
**for free** in the same Redis key that already
stores the checkpoint. No new storage layer.

Restart recovery: the dispatcher loads the checkpoint;
the cursor is already in the loaded view; the first
post-restart tick computes the delta from the cursor
to `view.last_event_id` and processes it.

---

## 3. Usage

A system that wants cursor-aware processing reads the
cursor at the start of `__call__`:

```python
from kntgraph.core.world.view import AgentView


class FSMSystem:
    """
    BusinessFSM (C-01) WorldSystem. Reads ``view.cursors``
    to detect which events have been processed since the
    last tick.
    """

    def __call__(self, world: "World") -> list[Event]:
        out: list[Event] = []
        for agent_id, view in self._agents(world):
            cursor_id = view.cursors.get("FSMSystem")
            last_id = view.last_event_id
            if cursor_id == last_id:
                continue  # happy path: nothing new since last tick

            # Delta-scan: re-derive triggers between
            # cursor_id+1 and last_id from the EventLog.
            triggers = self._resolve_triggers(
                view, cursor_id, last_id, world.event_log
            )
            for trigger in triggers:
                out.extend(self._emit_transition(view, trigger))
        return out
```

Systems that don't care about cursors (most existing
systems: `_BaseRoleSystem`, `RuleBasedChatSystem`,
`SagaSystem`, `SagaTimeoutSystem`) ignore the field.
`view.cursors.get("TheirName")` returns `None`, and
they proceed as before. The dispatcher still writes
the cursor for them (conservative: it costs nothing
and keeps the bookkeeping consistent).

---

## 4. Trade-offs

### 4.1 Per-system vs per-agent

We considered a single "last processed" cursor at
the agent level (one value, not a map). Rejected:
multiple systems may react to the same agent at
different rates; a single cursor would either be too
coarse (locks all systems to the same lag) or require
a single canonical owner (no current candidate).

### 4.2 Single value vs list

We considered `cursors: Mapping[str, list[str]]`
(all processed event_ids). Rejected:
- The FSM only needs the OLDEST cursor value to
  compute the delta (the new value is
  `view.last_event_id`).
- A list grows unbounded without GC.
- The EventLog is the source of truth for the gap;
  duplicating it in the view is denormalization.

### 4.3 Advance only on emit

We considered advancing the cursor whenever the
system is invoked (regardless of emit). Rejected:
- A system might be invoked but emit no events
  (e.g., it inspected the view and decided nothing to
  do — a valid pattern for idempotent systems).
- "Advance on read" requires the system to declare
  what it read, which adds API surface.
- For the FSM use case, "advance on emit" is fine:
  every transition (allowed or rejected) emits
  `fsm.transitioned` or `fsm.transition_rejected`, so
  the cursor advances in both paths.

Trade-off: a system that **silently** processes events
without emitting is not tracked. If such systems
exist, v2 needs an explicit
`system.acknowledge(agent_id, event_id)` API. Out of
scope for v1.

### 4.4 Storage location: AgentView vs separate map

We considered a separate per-system map maintained
by the dispatcher (e.g.,
`dispatcher._cursors: dict[(system, agent), str]`).
Rejected:
- Persistence would need a new storage layer.
- Cursor is read in the same context as the agent's
  other state — keeping it in the view is natural.

Trade-off: AgentView grows by ~80 bytes × N_systems
per agent. For 10k agents × 5 systems = ~4MB. The
WorldCheckpoint without cursors is already larger;
the increase is acceptable.

### 4.5 Class-name collisions

Two system classes with the same name in different
modules collide in the cursor key. Mitigation:
`__fsm_system_name__` ClassVar override. Documented
in §2.1.

---

## 5. Multi-event tick: end-to-end example

```
Tick T:
  EventLog:    E1 → E2 → E3 (all in the same batch)
  View:        last_event_id = E3
  Cursor FSM:  E0 (from previous tick)

  FSMSystem reads: cursor=E0, last=E3 → delta = 3 events
  FSMSystem scans EventLog: E1, E2, E3
  FSMSystem emits 0..N transitions
  Dispatcher: cursors["FSMSystem"] = E3

Tick T+1:
  EventLog:    E4 (new)
  View:        last_event_id = E4
  Cursor FSM:  E3

  FSMSystem reads: cursor=E3, last=E4 → delta = 1 event
  FSMSystem scans EventLog: E4
  FSMSystem emits transitions
  Dispatcher: cursors["FSMSystem"] = E4
```

---

## 6. Restart: end-to-end example

```
Tick T (process crashes mid-tick):
  EventLog:    ... → E5 (durably committed before crash)
  Checkpoint:  saved at end of tick T-1
               cursors["FSMSystem"] = E3

Restart:
  Dispatcher loads checkpoint: cursors["FSMSystem"] = E3
  View:        last_event_id = E5 (from the loaded view)

  FSMSystem reads: cursor=E3, last=E5 → delta = 2 events (E4, E5)
  FSMSystem scans EventLog: E4, E5
  FSMSystem emits transitions (idempotent: event_ids
  match what was already in the log; EventLog dedup
  catches duplicates if any slip through)
  Dispatcher: cursors["FSMSystem"] = E5
```

No loss, no duplication. ✓

---

## 7. Migration

### 7.1 Phases

- **PR 1 (this ADR's implementation)** — Add
  `cursors` field to `AgentView`. Modify
  `runner/_systems_runner.py` to advance the cursor
  after each system emits. No system uses the field
  yet; the migration is inert.
- **PR 2 (FSM migration)** — `BusinessFSMConcordo`'s
  `FSMSystem` reads `view.cursors.get("FSMSystem")`
  instead of maintaining an internal
  `dict[agent_id, last_processed_event_id]`. Removes
  ~10 lines of per-instance state.
- **Future** — `SagaSystem` may opt in if multi-event
  tick becomes a concern (ADR-072 §3.4 currently
  uses the last-event-only path; no migration needed
  today).

### 7.2 Compatibility

- Systems that don't read `cursors` are unaffected.
- The AgentView change is additive: existing
  field-by-field access patterns still work.
- Pickled WorldCheckpoints from before this change
  load correctly (the new field defaults to a plain
  `dict`; pickling a dict is well-tested and matches
  the existing `AgentView.components` behavior).

---

## 8. Open questions

1. **GC of stale cursor entries.** If a system is
   removed (a vertical migrates from one FSM config
   to another with a different name), the old cursor
   entry persists. Acceptable: harmless; bloat is
   bounded by `N_systems × N_agents`.
2. **System name collisions.** Subclasses with the
   same class name in different modules collide.
   Mitigation: `__fsm_system_name__` ClassVar override.
3. **Explicit acknowledge API.** Out of scope for v1.
   Tracked for v2 if any system needs to track reads
   without emit.
4. **Multiple dispatchers (multi-instance).** If the
   framework supports running multiple dispatchers for
   the same agent (e.g., sharded per-region), the
   cursor in the AgentView becomes a shared resource.
   Today the framework is single-instance per agent
   (ADR-035 covers sharding but not FSM/Saga
   cursors). Tracked for follow-up.

---

## 9. References

- Evans, E. *Domain-Driven Design*, 2003
- [Akka Persistence — recovery](https://doc.akka.io/docs/akka/current/typed/persistence.html)
- [ADR-018 — WorldSystem + ReactiveDispatcher](./ADR-018-WorldIncremental-WorldSystem.md)
- [ADR-069 — Concordo Foundation](./ADR-069-Agent-Concordo-Foundation.md)
- [ADR-071 — BusinessFSM Concordo](./ADR-071-BusinessFSM-Concordo.md)
- [ADR-072 — WorkflowSaga Concordo](./ADR-072-WorkflowSaga-Concordo.md)
