# ADR-045: Tool-call Request TTL (orphan eviction via sweeper system)

**Status:** Accepted
**Date:** July 14, 2026
**Related to:** [ADR-034](./ADR-034-ToolCall-ECS-Components.md), [ADR-036](./ADR-036-Tool-Worker-Pattern.md), [ADR-043](./ADR-043-LiteLLM-Worker-Migration.md), [ADR-044](./ADR-044-Tool-call-Overlay-Accumulation.md)

## 1. Context

ADR-044 introduced **completion-driven eviction** in
``overlay_tool_calls``: a ``tool_requests`` entry is
removed from the slot when the matching
``tool_completions`` entry lands in a subsequent
tick. This closes the multi-tick correlation problem
(the chat round-trip; the ``SolutionExtractor``; any
tool with non-trivial worker latency).

The eviction rule is **pure**: a request lives in
the slot until the completion arrives. In a healthy
deployment the completion **always** arrives
(eventually — the worker pool retries on transient
failures, escalates to a DLQ on hard failures). But
the framework has no termination guarantee:

  - **Worker crash**: if the ``ProcessPoolExecutor``
    process holding the worker dies (OOM, SIGKILL,
    machine reboot), the
    ``tool.<name>.completed`` event is **never**
    emitted. The ``tool.<name>.failed`` event is
    emitted only on **handled** exceptions
    (``Result.err_value``); an unhandled crash in
    the worker bypasses the failure path entirely.
  - **DLQ (dead-letter queue)**: the framework's
    reaper escalates a message to the DLQ after
    ``retries`` attempts (default 3) and emits a
    ``tool.<name>.failed`` event. In this case the
    completion DOES arrive, but the request has
    already been in the slot for the full retry
    budget (default 3 attempts × ``ack_timeout``).
  - **Stuck worker / hang**: a worker that hangs
    (e.g. a misbehaving ``invoke`` that never
    returns) holds the request indefinitely. The
    reaper does NOT reclaim it because the message
    is in the worker's PEL (Pending Entries List),
    not idle.
  - **Worker manager process restart**: if the
    ``WorkerManager`` process is restarted, in-flight
    requests (those whose ``xreadgroup`` consumed
    the message but did not yet ``xack``) are
    re-delivered to the next consumer. The original
    request stays in the ``tool_requests`` slot
    (the system already reacted to it). The
    duplicate delivery is **at-least-once**, which
    is fine; but the **first** delivery's request
    is still in the slot until the re-delivery's
    completion lands.

In all of these cases, the request becomes an
**orphan**: it lives in the ``tool_requests`` slot
forever (or until the world checkpoint is
discarded), consuming memory and (more importantly)
cluttering the system's view of the world. The
system might react to a stale ``tool_requests``
entry on a later tick (e.g. emit a fallback event
for a request whose completion already happened in
a different agent's view).

The current behaviour was documented as the
ADR-044 follow-up (DEBT.md §2.16 → §2.18 → open
**ADR-045 (TTL-based eviction for orphaned
requests)**).

## 2. Decision

Introduce a **per-request TTL** (``expires_at``)
on the ``ToolCallRequest`` component and have a
**separate ``WorldSystem``** —
:class:`ToolCallTTLSweeperSystem` — emit
``tool.<name>.failed`` events for stale requests.
The overlay (the pure projection that materialises
the ``tool_requests`` slot) **does not enforce the
TTL** — it only **sets** the ``expires_at`` field
on each new request. The separation keeps the
overlay pure (no clock injection, no I/O) and
makes the TTL enforcement observable (a system
that downstream consumers can subscribe to).

### 2.1 The ``expires_at`` field

A new field on ``ToolCallRequest``:

```python
@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    # ... existing fields ...
    # ADR-045: the wall-clock time at which the
    # request expires (UTC, timezone-aware).
    # Computed at materialisation time as
    # ``requested_at + ttl_seconds``. ``None``
    # means "no TTL" (legacy behaviour).
    expires_at: Optional[datetime] = None
```

The field is **set** by ``_build_request`` (a
private helper in
``core/world/projection_tool_calls.py``):

```python
def _build_request(
    event: Event, *, tool_name: str, ttl_seconds: float
) -> ToolCallRequest:
    requested_at = event.timestamp
    if ttl_seconds > 0:
        expires_at = requested_at + timedelta(seconds=ttl_seconds)
    else:
        expires_at = None
    # ... return ToolCallRequest(..., expires_at=expires_at)
```

A TTL of ``0`` (or negative) means **TTL disabled**
(``expires_at = None``). The overlay always sets
``expires_at`` (using the configured TTL); the
``None`` default is for forward-compat with old
``World`` checkpoints that may carry requests
without the field.

### 2.2 The ``ToolCallTTL`` config

A new immutable dataclass in
``core/world/components.py``:

```python
@dataclass(frozen=True, slots=True)
class ToolCallTTL:
    """Per-tool TTL for ``ToolCallRequest`` entries."""
    default_ttl_seconds: float = 300.0  # 5 minutes
    per_tool_ttls: Mapping[str, float] = field(
        default_factory=dict
    )

    def ttl_for(self, tool_name: str) -> float:
        return self.per_tool_ttls.get(
            tool_name, self.default_ttl_seconds
        )
```

The dispatcher accepts a
``tool_ttls: ToolCallTTL | None = None`` argument
(defaulting to ``None`` to preserve the legacy
behaviour). When the operator passes an explicit
``tool_ttls`` config, the dispatcher:

  1. Threads the ``ToolCallTTL`` to the overlay
     (the overlay uses ``ttl.ttl_for(tool_name)``
     to compute the ``expires_at`` for each new
     request).
  2. **Auto-registers** the
     :class:`ToolCallTTLSweeperSystem` (so the
     operator does not have to remember to add it
     to the ``systems=[...]`` list). The operator
     can opt out by passing an explicit
     ``ToolCallTTLSweeperSystem`` (the dispatcher
     dedupes) or by passing ``tool_ttls=None``.

### 2.3 The ``ToolCallTTLSweeperSystem`

A new ``WorldSystem`` in
``runner/tool_call_ttl_sweeper.py``:

```python
class ToolCallTTLSweeperSystem:
    """Sweep the ``tool_requests`` slot of every
    agent in the World and emit
    ``tool.<name>.failed`` for stale requests.
    """

    def __init__(
        self,
        *,
        now: Optional[datetime] = None,
        error_message: str = "ttl_expired",
    ) -> None: ...

    def __call__(
        self, world_or_views: "World | Mapping[str, AgentView]"
    ) -> list[Event]:
        events: list[Event] = []
        now = self._now or datetime.now(tz=timezone.utc)
        views = (
            world_or_views.views
            if isinstance(world_or_views, World)
            else world_or_views
        )
        for agent_id, view in views.items():
            tool_requests = view.components.get(
                "tool_requests", {}
            )
            for request_id, req in tool_requests.items():
                if not isinstance(req, ToolCallRequest):
                    continue
                if req.expires_at is None:
                    continue  # TTL disabled
                if now < req.expires_at:
                    continue  # Not yet expired
                if request_id in self._emitted_failures:
                    continue  # Already emitted
                self._emitted_failures.add(request_id)
                events.append(self._build_failed_event(...))
        return events
```

The emitted event is a domain event with the
standard failure shape:

```python
Event.create(
    event_type=f"tool.{tool_name}.failed",
    agent_id=agent_id,
    event_class="domain",
    data={
        "error": "ttl_expired",
        "request_event_id": str(request.event_id),
        "tool_name": tool_name,
        "expired_at": request.expires_at.isoformat(),
        "swept_at": now.isoformat(),
    },
    causation_id=UUID(request.request_event_id),
    correlation=CorrelationContext(
        correlation_id=request.correlation_id
    ),
)
```

The ``causation_id`` is the request's eid (the
same join key the ``WorkerManager`` uses for
completions). The ``correlation`` is derived
from the request's ``correlation_id`` so the
failure lives in the same flow as the request.

### 2.4 Why a separate system (not a sweeper thread)

The TTL enforcement was originally designed as a
sweeper thread or as inline eviction in
``overlay_tool_calls``. Both approaches had
structural problems:

  - **Sweeper thread**: an additional async task
    to schedule and clean up; risk of leaking
    the task on dispatcher restart; harder to
    test.
  - **Inline eviction in ``overlay_tool_calls``**:
    the overlay is a **pure** function (ADR-034);
    mixing in a wall clock breaks the purity and
    forces the overlay to walk every agent in
    ``base_views`` on every tick (which broke the
    "no allocation for non-tool batches"
    optimisation of ADR-044). The test
    ``test_overlay_ttl_evicts_carried_request``
    (in an earlier draft) failed because the
    overlay is a no-op for batches with no tool
    events.

The **sweeper system** separates concerns:

  - **Overlay** (pure): sets ``expires_at`` on
    each new request. No clock injection. No
    allocation for non-tool batches.
  - **Sweeper** (impure system): reads the wall
    clock, walks the views, emits failure events.
    The I/O is explicit (a system that produces
    events). The dispatcher auto-registers the
    sweeper when the operator opts in to TTL
    enforcement (via ``tool_ttls=...``).

The separation preserves the framework's
"projection as pure data" invariant (ADR-034) and
keeps the TTL enforcement observable (a downstream
consumer can subscribe to the ``tool.<name>.failed``
events for metrics, retries, etc.).

### 2.5 Why not a "stale request" event emitted by the overlay

A naive alternative is to have the overlay emit
``tool.<name>.expired`` events directly. This was
rejected:

  - The overlay is pure; emitting events breaks
    the purity.
  - The overlay is called once per ``fold_with_filter``
    invocation; the wall clock is not in scope.
  - The sweeper system is the right place for
    "I produce events"; the projection is "I
    produce data".

### 2.6 Dedup and idempotency

The sweeper is **stateful** (``_emitted_failures``
set): a request that stays in the slot across
multiple ticks (because the completion never
arrives) triggers **at most one** failed event.
The dedup is in-memory; a process restart
re-derives the dedup from the EventLog via the
``causation_id`` on subsequent events (a metrics
consumer can subscribe to ``tool.<name>.failed``
events it previously emitted to filter them out on
re-folds).

The sweeper **does not evict** the stale request
from the ``tool_requests`` slot. The eviction is
left to the **completion-driven rule** (ADR-044
§2.3 option 1): when the worker's completion
eventually arrives, the request is removed from
the slot. If the completion never arrives, the
request stays in the slot forever (memory leak);
a follow-up **GC_TICK** event (or a periodic
compaction pass) is the mitigation (out of scope
for ADR-045).

## 3. Consequences

### 3.1 Positive

  - **Bounded slot growth** (combined with
    periodic compaction in a follow-up ADR):
    the ``tool_requests`` slot has a TTL
    observed by the sweeper; requests older than
    the TTL trigger a failure event that
    downstream systems (e.g. ``SolutionExtractor``)
    can react to.
  - **Observable failures**: the sweeper emits
    ``tool.<name>.failed`` events; downstream
    systems (metrics, alerts, retries) can
    subscribe to the EventLog and observe the
    failure (no polling the slot).
  - **Per-tool tuning**: the operator can tune
    the TTL to match the latency profile of each
    tool (synchronous helpers get tight TTLs;
    batch tools get loose TTLs).
  - **Pure projection**: the overlay remains
    pure (no clock injection, no I/O). The
    ``tool.<name>.failed`` event is emitted by
    a system, not by the projection.
  - **Auto-registration**: the dispatcher
    auto-registers the sweeper when the operator
    passes a ``tool_ttls=`` config. No
    boilerplate.

### 3.2 Negative / risks

  - **TTL too short → false eviction**: if the
    operator sets a TTL shorter than the worker's
    latency (e.g. 1s for an LLM call that takes
    3s), the sweeper emits a failed event before
    the completion arrives. The completion is
    then an orphan and the system may react to
    both the failed event and the (late)
    completion. This is mitigated by the
    per-tool override and the 5-minute default
    (which is generous for most tools).
  - **TTL too long → stale requests linger**: a
    long-running tool (e.g. a 1-hour video
    transcoder) has a long TTL; the request sits
    in the slot until the completion arrives
    (potentially an hour). The slot memory is
    bounded by ``rate × TTL``. Mitigated by
    per-tool override and follow-up GC_TICK
    compaction.
  - **Dedup lost on restart**: a process restart
    re-derives the dedup from the EventLog (the
    sweeper re-emits for any stale request whose
    failed event is not in the log). Downstream
    consumers (e.g. metrics) MUST be idempotent
    (deduplicate by ``causation_id``) to avoid
    double-counting.
  - **Replay divergence**: a full replay (e.g.
    a re-fold of the entire EventLog) will trigger
    the sweeper for any stale request in the
    replayed history. This is **correct** (the
    request is stale; the sweeper should emit)
    but might surprise operators who expect the
    replay to reproduce the original sequence
    exactly. The mitigation is to disable the
    sweeper during replay (``dispatcher.start()``
    followed by a manual fold without the
    sweeper) or to use a custom ``ReactiveDispatcher``
    that does not auto-register the sweeper.

### 3.3 Operational guidance

  - **Default (5 minutes)**: safe for the
    majority of tools (LLM, HTTP, DB). Set this
    via the dispatcher's
    ``tool_ttls=ToolCallTTL()`` argument.
  - **Tight (1 minute)**: synchronous helpers
    (e.g. ``pii_redaction``, ``json_parse``,
    in-process DB queries). The completion lands
    in the same tick; the TTL is a backstop.
  - **Loose (1 hour)**: batch tools (e.g. video
    transcoders, large document parsers). The
    worker takes minutes; the TTL accommodates
    the latency.
  - **Per-tool override**: ``ToolCallTTL(
    per_tool_ttls={"pii_redaction": 60.0,
    "video_transcoder": 3600.0})``.

## 4. Implementation

### 4.1 ``ToolCallRequest.expires_at`` and ``ToolCallTTL``

Added in ``core/world/components.py``. The
``ToolCallTTL`` dataclass carries the per-tool TTL
config; ``ToolCallRequest.expires_at`` is the
wall-clock time at which the request expires.

### 4.2 ``_build_request`` and the overlay

The ``_build_request`` helper in
``core/world/projection_tool_calls.py`` computes
``expires_at = requested_at + timedelta(seconds=ttl_seconds)``.
The overlay threads the ``ToolCallTTL`` config
(``overlay_tool_calls(..., ttl=ToolCallTTL())``)
and uses ``ttl.ttl_for(tool_name)`` per request.

**The overlay does NOT enforce the TTL.** The
``overlay_tool_calls`` function returns the
``tool_requests`` slot with stale requests
intact; the sweeper system handles the eviction.

### 4.3 ``ToolCallTTLSweeperSystem``

New module ``runner/tool_call_ttl_sweeper.py``:

```python
class ToolCallTTLSweeperSystem:
    """Sweep the ``tool_requests`` slot of every
    agent in the World and emit
    ``tool.<name>.failed`` for stale requests."""
    ...
```

The system accepts either a ``World`` (production
path) or a ``Mapping[str, AgentView]`` (test path)
to keep the tests independent of the dispatcher's
fold pipeline.

### 4.4 ``ReactiveDispatcher`` auto-registration

The ``ReactiveDispatcher.__init__`` accepts
``tool_ttls: ToolCallTTL | None = None``. When the
operator passes an explicit ``tool_ttls`` config,
the dispatcher:

  1. Threads the ``ToolCallTTL`` to the overlay
     (the overlay sets ``expires_at`` on each
     new request).
  2. **Auto-registers** the
     :class:`ToolCallTTLSweeperSystem` (unless
     the operator has already passed one in
     ``systems=[...]``).

The auto-registration is opt-in (default
``tool_ttls=None`` preserves the legacy behaviour
of no TTL enforcement).

### 4.5 Tests

Nine unit tests in
``tests/unit/runner/test_tool_call_ttl_sweeper.py``:

  1. ``test_stale_request_emits_failed_event``:
     a request whose ``expires_at`` is in the past
     triggers a ``tool.<name>.failed`` event.
  2. ``test_fresh_request_emits_nothing``: a
     request whose ``expires_at`` is in the future
     does NOT trigger a failure.
  3. ``test_ttl_disabled_emits_nothing``: a
     request with ``expires_at=None`` is never
     emitted as failed.
  4. ``test_dedup_emits_once_for_same_request``:
     a request that stays in the slot across
     multiple ticks triggers AT MOST ONE failed
     event.
  5. ``test_dedup_scoped_to_instance``: two
     sweeper instances do NOT share dedup
     memory; a fresh instance re-emits.
  6. ``test_sweeper_handles_multiple_agents``:
     the sweeper walks every agent in the World
     and emits a failed event for each stale
     request.
  7. ``test_sweeper_emits_only_for_stale_in_multi_agent``:
     only stale requests trigger failures;
     fresh requests are preserved.
  8. ``test_empty_world_emits_nothing``: an
     empty World is handled gracefully.
  9. ``test_request_with_empty_tool_name_still_emits``:
     a request with the legacy bare form
     (``tool.requested``) still triggers a
     failure event (the sweeper does not crash
     on an empty tool name).

## 5. Alternatives considered

### 5.1 Sweeper thread / scheduled task

A background task in the dispatcher that wakes up
every N seconds and walks the
``tool_requests`` slot evicting expired entries.

  - **Pros**: precise TTL enforcement (no
    poll_interval staleness).
  - **Cons**: additional async task to schedule
    and clean up; risk of leaking the task on
    dispatcher restart; harder to test.
  - **Decision**: rejected in favour of the
    sweeper **system** (still poll-bound, but
    observable as a domain event).

### 5.2 Inline eviction in ``overlay_tool_calls``

Add Phase 3 (TTL eviction) to the overlay, after
the merge but before the slot install.

  - **Pros**: simpler than a separate system; no
    dispatcher plumbing.
  - **Cons**: breaks the overlay's purity
    (wall clock injection); forces the overlay
    to walk every agent in ``base_views`` on every
    tick (which broke the "no allocation for
    non-tool batches" optimisation of ADR-044);
    the test
    ``test_overlay_ttl_evicts_carried_request``
    (an earlier draft) failed because the
    overlay is a no-op for batches with no tool
    events.
  - **Decision**: rejected. The sweeper system
    keeps the overlay pure and the TTL
    enforcement explicit.

### 5.3 Time-bucketed slot (LRU-style)

A ``tool_requests`` slot organised as a time-
bucketed dict (e.g. one bucket per second). The
overlay evicts entire buckets whose timestamp is
older than ``now - TTL``.

  - **Pros**: O(1) eviction (drop the bucket).
  - **Cons**: complicated structure; harder to
    reason about; the completion-driven eviction
    is still per-entry.
  - **Decision**: rejected. The per-entry TTL is
    simpler and the per-entry cost is acceptable
    (the slot is small in practice; the bound is
    ``rate × TTL``).

### 5.4 Eviction on a separate "garbage collect" tick

A dedicated ``GC_TICK`` event type that the
dispatcher emits every N seconds; systems can
react to it to clean up their state.

  - **Pros**: explicit; systems can opt in.
  - **Cons**: pushes the problem to the systems
    (each system has to implement its own GC);
    duplicates the eviction logic across systems.
  - **Decision**: rejected. The sweeper system
    centralises the logic; downstream systems
    only react to ``tool.<name>.failed`` events.

## 6. Migration

  - **No breaking change**: ``expires_at``
    defaults to ``None`` (legacy behaviour).
    ``ToolCallTTL()`` defaults to a 5-minute
    global TTL. The dispatcher's ``tool_ttls``
    argument defaults to ``None`` (no
    auto-registration of the sweeper; legacy
    behaviour). Production deployments that
    pass ``tool_ttls=ToolCallTTL()`` opt in to
    the new behaviour automatically.
  - **Existing requests in old World
    checkpoints**: the ``expires_at`` field
    defaults to ``None``, so the sweeper
    ignores them. The completion-driven eviction
    (ADR-044) still applies.
  - **Replay divergence**: the sweeper auto-
    registers with the dispatcher. A pure replay
    (e.g. ``World.fold(events)``) does not run
    the sweeper; the operator must run the
    sweeper manually if they want the failed
    events to appear in the replayed history.

## 7. Acceptance checklist

  - [x] ``ToolCallRequest.expires_at`` field
    added (default ``None``).
  - [x] ``ToolCallTTL`` dataclass added in
    ``core/world/components.py``.
  - [x] ``_build_request`` computes
    ``expires_at`` from the configured TTL.
  - [x] ``overlay_tool_calls`` accepts
    ``ttl=ToolCallTTL()`` (no longer accepts
    ``now=...``; the overlay is pure).
  - [x] ``ToolCallTTLSweeperSystem`` created in
    ``runner/tool_call_ttl_sweeper.py``.
  - [x] The sweeper emits
    ``tool.<name>.failed`` for stale requests.
  - [x] The sweeper dedups via
    ``_emitted_failures``.
  - [x] The sweeper does NOT evict the stale
    request from the slot (the completion-
    driven rule handles that).
  - [x] ``ReactiveDispatcher`` accepts
    ``tool_ttls=ToolCallTTL()`` and auto-
    registers the sweeper.
  - [x] 9 unit tests in
    ``tests/unit/runner/test_tool_call_ttl_sweeper.py``.
  - [x] All existing tests pass (the new
    behaviour is opt-in via the dispatcher's
    ``tool_ttls`` argument; default is no TTL
    enforcement).
  - [ ] ``CHANGELOG.md`` updated.
  - [ ] ``DEBT.md`` §2.16 follow-up closed.
