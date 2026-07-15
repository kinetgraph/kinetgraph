<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-044: Tool-call Overlay Accumulation (slot persistence across ticks)

**Status:** Proposed
**Date:** July 14, 2026
**Related to:** [ADR-034](./ADR-034-ToolCall-ECS-Components.md), [ADR-036](./ADR-036-Tool-Worker-Pattern.md), [ADR-042](./ADR-042-Agents-Memory-Model-usage.md), [ADR-043](./ADR-043-LiteLLM-Worker-Migration.md)

## 1. Context

The `overlay_tool_calls` function in
`core/world/projection_tool_calls.py` (ADR-034) is
the *incremental* projection used by
`ReactiveDispatcher._fold_with_filter`. It is called
once per tick with the **current batch** of new
events; it walks the batch and rebuilds the
`tool_requests` / `tool_completions` slots from
scratch.

The accumulator it uses is local to the call:

```python
tool_requests: dict[str, dict[str, ToolCallRequest]] = collections.defaultdict(dict)
```

This means that when a tool's request and its
completion land in **different ticks** (e.g.
`request` in tick N, `completion` in tick N+1, K
ticks apart where K is the tool's latency), the
`tool_requests` slot is **dropped** in tick N+1 —
the batch of tick N+1 contains only the completion,
so the projection produces an empty `tool_requests`
slot. The system in tick N+1 cannot see the
pending request it emitted in tick N.

This was discovered during the example 05b rewrite
(see DEBT.md §2.16): the chat round-trip emits a
`tool.chat_llm.requested` in tick N, the LLM takes
0.3-0.5s, the `tool.chat_llm.completed` lands in
tick N+1, and the system in tick N+1 has no way to
match the completion to the request (the
`tool_requests` slot is empty).

The example 19 (tool worker pattern, ADR-036) does
**not** exercise the bug because the example has a
single turn and the request and completion land in
the same tick (the worker pool processes them in
parallel and the dispatcher reads the events in the
next poll). Production code that depends on
cross-tick correlation (the chat loop, the
`SolutionExtractor`, anything with a non-trivial
worker latency) **is** affected.

## 2. Decision

We change the contract of the tool-call overlay
from **per-batch rebuild** to **accumulate**:
`overlay_tool_calls` **merges** the new
requests/completions with the existing slots from
`base_views` rather than replacing them. The slot
becomes a *table* that the dispatcher reads as if
it were a database; eviction is by TTL or by
explicit clear.

### 2.1 What changes

The `overlay_tool_calls` function in
`core/world/projection_tool_calls.py`:

  - **Before:** walks the batch, builds local
    `tool_requests` / `tool_completions` dicts,
    installs them on the view.
  - **After:** walks the batch, builds local
    `tool_requests` / `tool_completions` dicts,
    **merges** them with the slots that already
    exist on the base view (if any), and installs
    the merged slots on the view.

The merge semantics are:

  - `tool_requests[agent_id][request_id] = req`
    uses the `request_event_id` as the key. The new
    request either matches an existing request
    (re-emitted, idempotent) or adds a new entry.
  - `tool_completions[agent_id][request_id] = comp`
    uses the `request_event_id` (the completion's
    `causation_id`) as the key. The new completion
    either matches an existing completion (re-emitted)
    or adds a new entry.

The merged dict replaces the existing slot on the
view. There is no eviction step at the overlay
level; eviction is the dispatcher's responsibility
(see §2.3).

### 2.2 What does NOT change

  - **`project_tool_calls` (full projection):** keeps
    the rebuild semantics. It is the *one-shot*
    fold for replays (no `base_views` to merge
    with). Documented in the function docstring.
  - **The `ToolCallRequest` / `ToolCallCompletion`
    data classes:** unchanged. The accumulation
    is at the *slot* level (`dict[request_id,
    component]`), not at the *component* level
    (each component is a frozen snapshot).
  - **The cross-tick correlation join:** unchanged.
    Systems continue to match completion to request
    by `request_event_id` (now correctly visible
    across ticks).
  - **`_has_tool_events` and the
    `_overlay_tool_projection` glue:** unchanged.
    The dispatcher still calls the overlay only
    when the batch contains tool events; the
    overlay is a no-op otherwise.

### 2.3 Eviction policy

The overlay accumulates. The slot grows. Two
policies for eviction:

  1. **Completion-driven eviction (default).**
     When a `tool.<name>.completed` event lands
     in tick N+1, the `tool_requests` slot
     entry for that `request_event_id` is
     **evicted** (the request has been answered;
     the system has reacted). The
     `tool_completions` slot entry remains
     until the next fold re-derives it (or is
     evicted by the dispatcher on a per-tick
     basis — see below).
  2. **TTL-driven eviction (optional).** The
     dispatcher can be configured with a
     `tool_slot_ttl_s` parameter; the overlay
     evicts entries older than the TTL. The
     default is **no TTL** (the slot grows until
     the agent's checkpoint is reaped).

For v0.8.0 we ship **option 1 (completion-driven
eviction of `tool_requests`)** only. The TTL
mechanism is a follow-up (ADR-045).

### 2.4 The `tool_completions` slot — keep or drop?

A `tool_completions` entry is **immutable** once
written (the completion is a fact; it doesn't
change). Two options:

  - **Keep all completions.** The slot grows
    monotonically. Simple, but unbounded.
  - **Drop on next system reaction.** The
    framework detects when a system has read the
    completion and reacted, and evicts the entry
    in the next tick. Requires a "read" signal
    from the system.

For v0.8.0 we ship the **keep-all** policy. The
slot is bounded in practice by the agent's
checkpoint TTL (set by
`IncrementalWorldStore.__init__(ttl_s=...)`); a
follow-up can add explicit eviction.

## 3. Consequences

### Positive

  - **Multi-tick correlation works.** Systems
    that emit a `tool.<name>.requested` in tick
    N can read the `tool_requests` slot in tick
    N+K and find their request. This unblocks
    the chat round-trip in example 05b and any
    other long-latency tool call.
  - **The system's view of the world is
    consistent.** The `tool_requests` slot in
    tick N+K contains the union of all requests
    the dispatcher has seen (minus those that
    were answered in an earlier tick).
  - **Replay is correct.** A checkpoint replay
    reconstructs the world by walking the
    cumulative events. The accumulated slots are
    the *expected* result of that walk.

### Negative

  - **Slot growth.** The `tool_requests` slot
    shrinks on completion (option 1) but the
    `tool_completions` slot grows monotonically
    (option "keep-all"). For long-running agents
    with many tool calls, the slot size can
    become O(events) in the worst case. The
    checkpoint TTL bounds the practical size.
  - **One subtle behaviour change.** A system
    that re-emits the same `tool.<name>.requested`
    in two ticks (e.g. because the first one was
    filtered out) now sees the request in the
    slot for both ticks. The system must be
    idempotent (use `is_new_intent` /
    `recorded_turns` sets) or risk emitting a
    second `session_recorder` call. **This is
    already the case today** (the example 05b
    has the `_processed_intents` set for this
    reason); the accumulation just makes the
    race more visible.
  - **The "rebuild" semantics of `project_tool_calls`
    (full projection) is no longer the same as
    the overlay.** The two functions diverge in
    their contract. The full projection rebuilds
    from the full event batch; the overlay
    accumulates. Both are correct for their
    callers (the dispatcher for the overlay; the
    replay path for the full projection).

### Mitigations

| Problem | Mitigation |
| --- | --- |
| Slot growth | The agent's checkpoint TTL bounds the practical size. A future ADR-045 adds a `tool_slot_ttl_s` parameter. |
| Duplicate `request` events | The system uses `_processed_intents` / `_recorded_turns` sets (already standard). Documented in the example 05b. |
| Documentation drift | The docstrings of `project_tool_calls` and `overlay_tool_calls` are explicit about their semantics. The first paragraph of each function is the contract. |

## 4. Migration Inventory

### Code (this commit)

  - `src/kntgraph/core/world/projection_tool_calls.py`:
    - `overlay_tool_calls` merges new
      requests/completions with the existing
      slots from `base_views` before installing
      on the view.
    - Add eviction: when a completion lands,
      evict the matching `tool_requests` entry.
  - `tests/unit/runner/test_reactive_tool_projection.py`:
    - New test: request in tick N, completion
      in tick N+1 → `tool_requests` slot
      preserved (now evicted on completion);
      `tool_completions` slot populated.
  - `examples/05b_session_chat_ecs.py`:
    - Remove the workaround that cached the
      `_latest_intent_eid` and the
      `_processed_intents` re-check. The system
      can now rely on the accumulated slot to
      find the request across ticks.
  - `DEBT.md`:
    - Section 2.16 is closed (status: Delivered
      via ADR-044 + this commit). Move to a new
      section 2.18 (current state).

### Follow-up

  - **ADR-045 (TTL eviction).** Add a
    `tool_slot_ttl_s` parameter to the dispatcher
    for long-running agents. Deferred — the
    checkpoint TTL is sufficient for v0.8.0.
  - **ADR-046 (drop-on-read).** Evict
    `tool_completions` entries after the system
    has reacted. Requires a "consumed" signal.
    Deferred — not needed for the chat use case.

## 5. Acceptance Checklist (Proposed → Accepted)

  - [ ] `overlay_tool_calls` merges new
    requests/completions with the existing slots
    from `base_views`.
  - [ ] When a completion lands, the matching
    `tool_requests` entry is evicted (option 1).
  - [ ] Regression test:
    `test_request_remains_visible_until_completion_arrives`
    in `test_reactive_tool_projection.py`.
  - [ ] Example 05b runs end-to-end against a real
    LLM (ollama): the `final session: 0 messages`
    issue is gone; the session has 6 messages
    (3 user + 3 assistant).
  - [ ] The `test_reactive_tool_projection.py`
    suite still passes (no regression in the
    single-tick case).
  - [ ] DEBT.md §2.16 is closed and replaced by
    §2.18 (current state).

When all boxes are checked, ADR-044 is `Accepted`.

## 6. Open Questions

1. **What is the right eviction key?** We evict by
   `request_event_id` (the completion's
   `causation_id`). A duplicate completion (e.g.
   from a worker retry) should be idempotent —
   the projection treats it as the same
   completion. **Decision:** key by
   `request_event_id`; duplicate completions
   overwrite (the framework's contract is
   at-least-once delivery, so duplicate
   completions are expected; the overwrite is
   idempotent because the `ToolCallCompletion`
   is a frozen dataclass with the same content).
2. **What about `tool.<name>.failed`?** The
   overlay treats `.failed` as a completion (with
   `status="failed"`). The eviction policy is the
   same (evict the matching `tool_requests`).
3. **What about `args_invalid`?** Same as
   `.failed`: the overlay treats it as a
   completion (the projection's
   `_completion_status` returns the appropriate
   status). Eviction is the same.
