<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-086: The domain declares a retry; the framework distinguishes a retry from a replay

- **Status:** Proposed
- **Date:** 2026-10-25
- **Author:** kinetgraph architecture team (raised by the operator: *"e se o request tiver uma prioridade como `try_again` configurada pelo domínio de negócio quando a retentativa de execução for mandatória"*)- **Related to:**
  - [ADR-085](./ADR-085-Tool-Request-Identity-and-Reaper-Deadline.md) — the request-identity problem this ADR completes. ADR-085 §3.4 derives a stable identity for the **first** request of a trigger; this ADR covers the **subsequent** request of the same trigger — the deliberate retry. The two are the two halves of one contract.
  - `soldi/backoffice` — the vertical that already uses the contract informally (§1.6). Its diagnostic: `docs/features/012-diagnostico-duplicacao-execucao-tools.md`.
  - [ADR-069](./ADR-069-Agent-Concordo-Foundation.md) — the Concordos foundation (FSM + Saga). §6 evaluates this ADR's feature against both modules and records what they already do.
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — the `ToolCallRequest` / `ToolCallCompletion` ECS slots. §3.3 uses the completion slot as the join that distinguishes a retry from a replay.
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — the Tool Worker Pattern. §3.2 places the retry declaration on the request wire, where the worker already reads it.
  - [ADR-075](./ADR-075-Tool-Task-Observability-and-Recovery.md) — the recovery tier. §3.4 is where the failure classification this ADR consumes is already produced.
  - [ADR-045](./ADR-045-Tool-Call-Request-TTL.md) — the Tool Call TTL. §3.4 explains why the TTL sweeper is not a retry mechanism.
  - [ADR-074](./ADR-074-Per-System-Cursors-in-AgentView.md) — the per-system cursor. §6.2 records the FSM's cursor gate as the framework's existing replay-safety mechanism.
  - [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) §3.1 — `JsonValue` for wire payloads. §3.2's new fields are `JsonValue`-typed.
  - [`kntgraph-typed-errors`](../.agents/skills/kntgraph-typed-errors/SKILL.md) §6.2 — mutating storage adapters return `Result[T, E]`. The worker's retry check follows the convention.
  - [`kntgraph-testing`](../.agents/skills/kntgraph-testing/SKILL.md) §7.1 — behaviour tests over the real `WorkerManager` and the real event log. §5 enumerates the coverage gate.

---

## 1. Context

### 1.1 The symptom

ADR-085 establishes that the framework must tell a **replay** (the
same request emitted again by mistake) from a **retry** (the same
request deliberately emitted again after a failure).

ADR-085's §3.4 fixes the replay half by deriving a stable identity
from the request's trigger. It explicitly leaves the retry half open:
if the identity is stable, a deliberate retry would be **collapsed
into the original** — the retry would never execute.

So the two halves need one contract, and this ADR is the second half.

**The production measurement (2026-10-25) sharpened the problem.**
The backoffice's re-emitter already anchors its `event_id` on a
stable trigger (§1.6), so its replays collapse correctly — and the
residual defect there is a *retry* that must **not** collapse. The
`attempt` counter is what separates them, and it is already on the
wire in one vertical, informally.

Without a framework-level definition, the next vertical will either
omit the counter (the retry collapses and silently never runs — a
lost business transaction) or invent a different spelling (the
framework cannot help it).

### 1.2 What the framework already knows about failures

The framework already classifies tool failures — and the classification
is already on the wire:

`core/result/errors.py:39`:
```python
ToolErrorClass = Literal["transient", "permanent", "unknown"]
```

`core/result/errors.py:20-28`:
> `ToolError` carries an `error_class` slot (`Literal["transient",
> "permanent", "unknown"]`) so the framework classifies failures
> without parsing the free-form message — `permanent` means the
> failure will never succeed on retry (e.g. `NoSuchBucket`);
> `transient` means retry may succeed.

The `.failed` event carries it (`tools/_message_handlers.py:288-298`):

```python
data={
    "error": result.error,
    "error_class": result.error_class,
}
```

And the docstring of the builder that produces it
(`tools/_message_handlers.py:281-286`) names the intended consumer:

> `data` carries both the human-readable `error` string and the
> framework-level `error_class` (transient / permanent / unknown).
> Downstream consumers (TTL sweeper, circuit breaker, DLQ adapter,
> **reactive agent**) act on `error_class` without parsing the
> free-form message.

`tools/_result.py:65-71`:
> `error_class` lets the WorkerManager, TTL sweeper, and circuit
> breaker act on the failure type without parsing the free-form
> `error` string. `transient` = retry may succeed; `permanent` =
> will never succeed.

### 1.3 The gap: nothing reads `error_class` to decide a re-emit

Audited across `src/kntgraph/`: `error_class` is **written** to the
wire in six places and **read** by none. There is no production code
that consumes `error_class` to decide whether to re-emit a request.

The intent was documented but never implemented —
`tests/unit/tools/test_manager.py:471-475`:

> A tool that classifies as `transient` keeps the classification on
> the wire — **the reactive agent reads `error_class` and decides
> whether to re-emit (with `idempotency_key` so the EventLog
> dedupes).**

Note the parenthetical: the original design already assumed the
**stable identity** of ADR-085 §3.4. The two halves of the contract
were designed together; only one was built.

The `WorkerManager`'s reaper uses a *different* signal —
`@tool_worker(idempotent=False)`, a **static** per-tool declaration
(`tools/_reaper.py`, `tool_is_idempotent`). That flag answers "may
this tool be re-run at all?", not "should this request be re-run?".

### 1.4 The alternative that was rejected: a content hash

An obvious approach is to deduplicate by hashing the request's
content — `(agent_id, tool, params)` — at consumption time. The
worker already holds the whole payload, so no new index is needed
and the duplicate is caught exactly where it would otherwise be
executed.

It is rejected. A content hash cannot distinguish:

| Situation | Content hash | Correct answer |
| --- | --- | --- |
| Replay (the incident) | identical to the original | **skip** |
| Legitimate retry after a transient failure | identical to the original | **execute** |
| The same tool called twice with the same params, deliberately | identical to the original | **execute** |

The hash collapses all three. The discriminator is not the content —
it is **the intent to retry**, which only the domain knows.

### 1.5 Why the domain must decide

The domain knows whether a retry is *mandatory* ("the payment must
be re-sent") or *forbidden* ("the duplicate invoice must never be
issued"). The framework does not. A framework-inferred policy would
have to guess, and a wrong guess in either direction is a business
incident: a dropped mandatory retry, or a repeated side effect.

ADR-066 already places three authorisation gates around tool
execution; the retry decision is a fourth, and it belongs to the
same authority — the domain.

### 1.6 The contract already exists in a vertical — this ADR formalises it

The production measurement of the backoffice (2026-10-25) found that
the `attempt` field and the `idempotency_key` anchor described below
are **already in use** — informally, by one vertical, with no
framework-level contract behind them.

`soldi/backoffice/src/backoffice/contexts/onboarding/events/tool_document_text_extractor.py:65-73`:

```python
computed_event_id = event_id
if computed_event_id is None and idempotency_key is not None:
    computed_event_id = generate_deterministic_event_id(
        causation_id=causation_id or "root",
        agent_id=agent_id,
        event_type=REQUESTED_EVENT_TYPE,
        data=data,
        idempotency_key=idempotency_key,     # <- excludes causation_id
    )
```

and the caller
(`.../systems/document_parser_completed_dispatch_system.py:112-129`):

```python
payload: DocumentTextExtractorRequestedPayload = {
    "tool": "document_text_extractor",
    "params": {..., "attempt": 1},
    "attempt": 1,                              # <- the attempt field
}
events_to_emit.append(
    tool_document_text_extractor_requested(
        ...
        idempotency_key=f"{agent_id}:{parser_result.source_path}",  # <- the trigger anchor
    )
)
```

Two things follow:

1. **The design is validated in production.** A vertical reached
   this shape independently, and it works: the request's `event_id`
   is stable across reactive wakes, so the EventLog and ADR-083's
   queue dedup both collapse the replays.
2. **The contract is undocumented and unenforced.** `attempt` is a
   plain `TypedDict` field in one vertical's payload — the framework
   neither reads it nor guarantees it. The next vertical will
   invent its own spelling, and a request that carries `attempt`
   without a stable anchor will be indistinguishable from a replay.

This ADR gives that emergent contract a framework-level definition.

---

## 2. The decision

> **A tool request MAY declare itself a retry. The declaration is
> explicit, travels on the request's wire payload, and is set by the
> domain. The framework MUST NOT infer a retry from the request's
> content, and MUST NOT re-emit a request on its own.**

Two distinct declarations, with distinct meanings:

| Declaration | Meaning | Identity |
| --- | --- | --- |
| *(absent)* | The first request for a trigger. A replay of it is a **replay** and is collapsed. | Derived from the **trigger** (ADR-085 §3.4) |
| `try_again: true` | An explicit, deliberate retry of the same logical request after a failure. | Derived from the **trigger + attempt** — never equal to the original |

### 2.1 The wire contract

The request's `data` gains two optional fields:

```json
{
  "tool": "document_text_extractor",
  "params": {"document_id": "..."},
  "try_again": true,
  "attempt": 2
}
```

- `try_again: bool` — the domain's explicit declaration that this
  request is a retry of a previous one. Absent (or `false`) means
  "first request for this trigger".
- `attempt: int` — the 1-based attempt number. Absent means `1`.

Both are **optional** and **additive**. A request without them keeps
today's exact wire format — the same compatibility rule ADR-083 §3.3
used for the dedup index.

### 2.2 The identity rule

> A request's identity is derived from `(trigger, attempt)` — never
> from `(trigger)` alone.

`attempt` is what separates a retry from the original. With
`attempt` in the hash:

| Request | Trigger | Attempt | Identity |
| --- | --- | --- | --- |
| original | T1 | 1 | `H(T1, 1)` |
| replay of the original | T1 | 1 | `H(T1, 1)` — **collapsed** |
| deliberate retry | T1 | 2 | `H(T1, 2)` — **distinct** |

The replay collapses (same attempt). The retry does not (different
attempt). The EventLog's existing `knt:eventids:<id>` index does the
work; no new index is introduced.

**The framework does not invent the attempt number.** A domain that
declares `try_again` states which attempt it is; the framework
rejects a retry whose attempt number is not greater than the one it
has already seen for that trigger (§3.4).

### 2.3 Where the declaration is made

`ToolAwareSystem.request_tool` gains the two parameters. The
framework's helper derives the attempt-independent identity; the
domain supplies the attempt.

```python
e = self.request_tool(
    agent_id=agent_id,
    tool_name=self.TOOL_NAME,
    params={...},
    causation_id=str(last_eid),
    correlation=correlation,
    try_again=True,     # this is a retry
    attempt=2,          # of what the domain already tried
)
```

### 2.4 The worker's obligation

The worker already derives the execution key
(`tools/manager.py:622`):

```python
idempotency_key = str(request_event.event_id)
```

With §2.2 in place, that key is now **stable across a replay and
distinct across a retry** — which is exactly the contract the tool
needs to be safe to re-invoke. No change is required in the worker
for the *identity*.

What the worker **must not** do is re-emit. The framework never
re-emits a request on its own: the reaper reclaims a *pending* message
(the same stream entry), it does not create a new one.

---

## 3. The failure path

### 3.1 The failure already carries the classification

`tools/_message_handlers.py:288-298` emits
`tool.<name>.failed` with `data["error_class"]`. The domain reads it
and decides.

### 3.2 The domain's re-emit

The domain sees `tool.<name>.failed` with
`error_class == "transient"`, decides the retry is mandatory, and
re-emits the request with `try_again=True, attempt=N+1`.

This is the reactive agent's job — the same role the role systems
already play. The framework provides the vocabulary; the domain
provides the decision.

### 3.3 The join that confirms the retry

The domain needs to know *which* request failed. The framework
already models that join:

- `ToolCallRequest.request_event_id` — the key of the request slot
  (`core/world/components.py:135`).
- `ToolCallCompletion.request_event_id` — the same key
  (`core/world/components.py:193`); ADR-034 joins the two.

The `.failed` event's `causation_id` is the request's `event_id`
(`tools/_message_handlers.py:292`). So the domain has an exact join
from a failure back to the request it answers — no new mechanism is
needed, and no content matching is involved.

### 3.4 The sweeper is not the retry mechanism

The Tool Call TTL sweeper (`runner/tool_call_ttl_sweeper.py`) emits a
failure for a request that never completed. That failure is a
**timeout**, and a timeout is `permanent` by construction: the
deadline has passed, so re-running the same attempt would only
produce the same timeout.

A retry of a timed-out request is a **new attempt** with a **new
deadline**, and the domain must declare it (`try_again=True,
attempt=N+1`). The sweeper never re-emits a request — it only
reports the timeout (§3.4 of ADR-085 confirms the sweeper's scope).

### 3.5 The permanent-failure case

`error_class == "permanent"` means the failure will never succeed on
retry. A domain that re-emits anyway is overriding the framework's
classification — which is allowed (the domain is the authority), but
the override should be visible. The framework therefore records the
attempt number in the request's `data`, so the audit trail shows
that attempt 2 was issued after a `permanent` failure.

---

## 4. What this does NOT change

- **The reaper** — ADR-085 §3.1–§3.3 are unaffected. A retry is a new
  request with its own TTL; the reaper's per-message deadline applies
  to it exactly as to the original.
- **The EventLog** — no new index. `knt:eventids:<id>` already keys
  on the derived `event_id`.
- **The tool queue** — no new index. ADR-083's dedup index (opt-in)
  still works, and now becomes *more* useful: a replay and a retry
  have different `event_id`s, so the index distinguishes them.
- **The `ToolCallRequest` / `ToolCallCompletion` slots** — unchanged.
  `attempt` lives on the request's `data`, not on the slot; the slot
  records what was requested, and `attempt` is a property of the
  request *event*.
- **The role systems' re-emit gate** — ADR-085 §3.6. With a stable
  identity the replay is collapsed at the EventLog, so the gate is an
  optimisation. This ADR does not depend on it.

---

## 5. Evaluation against Concordos (FSM and Saga)

The operator asked whether this feature has a home in the FSM and the
Saga. Audited both.

### 5.1 What the Saga already does — the precedent

`concordos/saga/_timeout_system.py:100-112` builds the saga timeout
event with a **deterministic `event_id`**:

```python
data: dict[str, JsonValue] = {
    "saga_id": saga.saga_id,
    "elapsed_ms": elapsed_ms,
    "stuck_at_step": saga.current_step,
    "timeout_ms": config.saga_timeout_ms,
}
eid = generate_deterministic_event_id(
    causation_id="root",
    event_type=event_type,
    data=data,
    agent_id=view.agent_id,
)
```

The module docstring (`_timeout_system.py:10-13`):
> The emitted event's `event_id` is deterministic (via
> `generate_deterministic_event_id`) so a tick that re-derives the
> same timeout produces the same event and is deduped by the
> EventLog's idempotency check.

**This is the same contract as ADR-085 §3.4, already implemented in
the framework.** The Saga solves "a tick re-derives the same event"
by deriving the identity from the *content* rather than from the
cause. ADR-085 §3.4 solves it by deriving it from the *trigger*.

Two observations:

1. The Saga's approach confirms that **deterministic identity is the
   framework's idiom** for this class of problem. ADR-085 §3.4 is
   not an invention; it is the established pattern applied to the
   tool-request path.
2. The Saga's `data` includes `elapsed_ms`, which **drifts between
   ticks**. Measured:

   ```
   Saga pattern - stable across re-derivation: True
   Saga pattern - stable when a data field drifts: False
   ```

   So the Saga's dedup holds only while the derived data is
   byte-identical. The saga timeout fires once per saga (the saga
   leaves the forward direction on timeout), so the drift does not
   bite in practice — but it shows that **a drifting field in the
   hash defeats the dedup**. The tool-request path must keep its
   identity inputs stable: the trigger, the attempt, the tool, and
   the params — and must **not** include a timestamp.

   This is a constraint ADR-085 §3.4 must honour: the request's
   `data` gains `ttl_remaining` (ADR-085 §3.2), which is a
   **wall-clock-derived, monotonically decreasing** value. If it
   entered the identity hash, the identity would drift exactly like
   `elapsed_ms`. It must not.

### 5.2 What the FSM already does — the other precedent

`concordos/fsm/_system.py:149-157` uses a **cursor gate**:

```python
# ADR-074: replay-safety / idle-tick fast path. The
# dispatcher advances ``view.cursors["FSMSystem"]``
# to ``view.last_event_id`` after every tick the FSM
# ran. If the cursor already matches the latest
# event the view has seen, there is nothing new to
# process.
cursor_id = view.cursors.get(_FSM_CURSOR_KEY)
if cursor_id is not None and cursor_id == view.last_event_id:
    return []
```

The FSM also filters its own output (`_system.py:183-184`):
> Skip FSM-emitted events (replayed through the EventLog on
> subsequent ticks). Cursor gating ALREADY handles dedup for events
> we ourselves emitted.

So the framework has **two** replay-safety mechanisms already:

| Mechanism | Where | How |
| --- | --- | --- |
| **Deterministic identity** | Saga timeout (`_timeout_system.py`) | the same content derives the same `event_id`; the EventLog dedupes |
| **Cursor gate** | FSM (`_system.py:149-157`) | the system declines to act on a trigger it has already processed |

Both are the *same answer* to the same question — "have I already
handled this?" — expressed in the two idioms the framework has.

### 5.3 What this means for the tool-request path

The tool-request path currently has **neither**. That is the gap.

- The **Saga's idiom** (deterministic identity) is the right one for
  the tool request, because the request must be routed to the queue
  and a cursor cannot gate a *fan-out* — the router has no cursor.
  ADR-085 §3.4 adopts this idiom.
- The **FSM's idiom** (cursor gate) is the right one for ADR-085 §3.6
  (the re-emit gate at the system that re-emits), because that system
  *does* have a cursor (`view.cursors[system_name]`, ADR-074).

So the two existing mechanisms map onto the two halves of the
contract, and this ADR's `attempt` field slots into the Saga's idiom
as the discriminator between "same content" (replay) and "next
content" (retry).

### 5.4 Should the Saga and the FSM use `try_again`?

**No — not as a retry mechanism.** The reason is that the Saga and
the FSM *are* the retry mechanism, and they already own it:

- The **Saga** compensates on failure and advances on success; a
  failed step is a domain decision expressed in the saga spec
  (`concordos/saga/_config.py`, `SagaStepConfig`), not a tool
  request retry. The saga's compensation is a **different** action
  from "run the same tool again".
- The **FSM** transitions on triggers; a `tool.<name>.failed` event
  is a trigger that may drive a transition (possibly to a retry
  state). The FSM's retry is a **state transition**, not a repeated
  tool request.

Where `try_again` *would* apply to a saga or an FSM step is when the
step's **tool** must be re-invoked — for example a saga step whose
tool failed transiently and the saga spec says to try that step
again. In that case the saga step emits the tool request with
`try_again=True` and the next attempt number. That is **adoption**,
not a change to the FSM/Saga semantics: the saga keeps owning the
decision, and uses this ADR's vocabulary to express it.

### 5.5 The useful interaction: the Saga already models the join

The saga's step-matching (`concordos/saga/_system.py:286-300`) uses
exactly the join §3.3 describes:

> The join uses the `tool_requests` slot (ADR-034): the
> `ToolCallRequest` carries the `tool_name` and the
> `request_event_id`; the `ToolCallCompletion` is keyed by that same
> `request_event_id` in the `tool_completions` slot.

So a saga step that wants to retry a transiently-failed tool already
has everything it needs: the failed request's `request_event_id` from
the completion slot, and the saga's own step state to decide whether
a retry is mandatory. §2.1's fields give it the vocabulary.

### 5.6 Verdict

| Question | Verdict |
| --- | --- |
| Does the FSM/Saga need `try_again`? | **No.** They own their own retry semantics (compensation / state transition). |
| Can they *use* it? | **Yes** — for a step whose **tool** must be re-invoked. The saga spec decides; the vocabulary expresses it. |
| Does this ADR duplicate an existing mechanism? | **No.** It completes the pair (ADR-085 §3.4 = replay; this ADR = retry) using the framework's own deterministic-identity idiom (the Saga's). |
| Does the FSM's cursor gate apply here? | **No** for the router (no cursor); **yes** for ADR-085 §3.6 (the re-emitting system has one). |

---

## 6. Alternatives considered

| Alternative | Pros | Cons | Verdict |
| --- | --- | --- | --- |
| **A. Dedupe by content hash `(agent_id, tool, params)`** | No new index; the worker already has the payload; catches the duplicate where it would execute | Collapses a legitimate retry with a replay (§1.4); the content is identical in all three cases | **Rejected** — cannot distinguish the cases |
| **B. Dedupe by `correlation_id`** | Already on the wire | The `correlation_id` is the **flow id** (`core/event/correlation.py:57`), stable across a whole conversation. Two different requests in the same flow share it — collapsing them loses real work | **Rejected** — the wrong granularity |
| **C. Let the framework infer the retry from `error_class`** | Automatic; no domain change | The framework cannot know whether the retry is *mandatory* or *forbidden* for the business (§1.5); the operator's instruction is that the domain decides | **Rejected** — the authority is the domain's |
| **D. Have the framework re-emit automatically on `transient`** | One place; no domain code | Same objection as C, and it puts the framework in the business-policy path | **Rejected** |
| **E. Encode the attempt in the `causation_id`** | No new field | `causation_id` is the *cause*; overloading it destroys the audit chain and ADR-037's correlation semantics | **Rejected** — the fields have distinct meanings |
| **F. A separate event type `tool.<name>.retry_requested`** | Unambiguous on the wire | A second event type doubles the surface the router, the worker, the projections and the ECS overlay must all learn — for a distinction one optional field carries | **Rejected** — a field, not a type |
| **G. (this ADR) `try_again` + `attempt` on the request's `data`; identity derived from `(trigger, attempt)`** | Explicit; the domain decides; no new index; reuses the EventLog's dedup; reuses the Saga's deterministic-identity idiom; the worker needs no change | The domain must supply the attempt number; a domain that miscounts would re-emit a replay | **Recommended** |

### 6.1 A note on the alternative that the framework itself almost shipped

`agents/role_systems/_prompts.py:107-125` already contains a
stable-key helper:

```python
def chat_idempotency_key(*, session_id, history_length, new_message) -> str:
    """Stable cache key for a chat turn."""
    h = hashlib.sha256(
        f"{session_id}|{history_length}|{new_message}".encode()
    ).hexdigest()
    return f"chat:{h[:32]}"
```

It is exported (`_prompts.py:285`) and **never called**. It is the
Saga's idiom applied to the chat turn. ADR-085 §3.4 should use it
(or its generalisation) rather than invent a third mechanism.

---

## 7. Compatibility

- **The wire format is unchanged for a first request.** The two new
  fields are optional and absent by default — the same additive rule
  ADR-083 §3.3 used for the dedup index.
- **Historical event ids are unaffected.** The identity derivation
  changes only for tool requests, and only where §2.2's rule applies.
- **The `ToolCallRequest` slot is unchanged** (ADR-034's frozen
  dataclass keeps its shape).
- **No new index, no new Redis key, no new command.** The EventLog's
  `knt:eventids:<id>` already provides the dedup; this ADR only
  changes *which* id a request carries.
- **The `ToolRouter`'s wire format is unchanged** — the fields ride
  inside the request event's existing `data` payload.

---

## 8. Tests

### 8.1 The retry/replay discrimination

1. **A replay collapses.** The same trigger, the same attempt → the
   same `event_id` → the EventLog dedupes → one queue entry.
2. **A retry does not collapse.** The same trigger, a higher attempt
   → a different `event_id` → two queue entries, two executions.
3. **The worker receives distinct keys.** The `idempotency_key`
   passed to `invoke` differs between the original and the retry, and
   matches between the original and its replay.
4. **The join works.** The domain can go from a `.failed` event's
   `causation_id` to the request slot's `request_event_id` and find
   the completion — no content matching.

### 8.2 The failure path

5. **`transient` is on the wire** and readable — the existing
   `test_err_transient_propagates_to_wire` covers the production half.
6. **`permanent` does not produce a retry** unless the domain
   declares one — and the attempt number makes the override visible.
7. **The sweeper's timeout is `permanent`** and is never re-emitted
   by the framework.

### 8.3 The FSM/Saga interaction

8. **A saga step that retries its tool** emits `try_again=True` with
   the next attempt number; the request is routed once per attempt.
9. **The FSM's cursor gate is unaffected** by a retry: the retry is a
   new trigger, not a re-processing of the old one.

---

## 9. Migration path

1. **ADR-085 §3.4 lands** — the trigger-derived identity. The replay
   collapses. *Without this step, a retry would also collapse and
   never execute* — so this step is a **prerequisite** for this ADR.
2. **This ADR lands** — `try_again` / `attempt` on the request's
   `data`; the identity becomes `(trigger, attempt)`.
3. **The domains adopt it** — the vertical that needs a mandatory
   retry sets `try_again=True` and the attempt number. The framework
   change is inert until a domain uses it.
4. **A saga or FSM step that needs a tool retry** adopts the
   vocabulary in its own step spec (§5.4).

---

## 10. Open questions for review

1. **Who supplies the attempt number?** The domain knows how many
   times *it* has asked. But the domain's count is per-process, and a
   restarted dispatcher loses it. The alternative is the framework
   deriving the attempt from the request slot (the `tool_requests`
   slot holds every request for the agent — the framework could count
   the prior attempts for the trigger). **Recommendation:** the domain
   supplies it, because only the domain knows whether a *previous
   attempt that never reached the log* counts. The framework must
   tolerate a gap. Reviewer may veto.
2. **Does the attempt belong in the `data` or in the slot?**
   `data` travels to the worker (which needs it); the slot is the
   agent's projection. **Recommendation:** `data` only — the slot is a
   projection of the event, and the event is the source of truth.
   Reviewer may veto.
3. **Should `try_again` be rejected when no prior failure exists?**
   A `try_again=True` with no matching `.failed` event is a domain
   bug (or a lost failure). The framework could reject it. But the
   framework cannot see the whole history cheaply, and the failure may
   have been TTL-evicted. **Recommendation:** do not reject; the
   attempt number in the audit trail is enough. Reviewer may veto.
4. **Does the reaper need to know about `attempt`?** A retry is a new
   request with a fresh TTL (ADR-085 §3.2), so no. But the *retry
   budget* — how many attempts a trigger may have — is a domain
   policy. **Recommendation:** out of scope for this ADR; the saga
   spec is the natural home for a step's attempt budget. Reviewer
   decides.
5. **Should the role systems set `attempt` automatically?** The role
   systems re-emit on replay (ADR-085 §3.6), which is the *replay*
   case, so they must NOT increment the attempt. The increment belongs
   to the domain's retry path. **Recommendation:** the role systems
   never set `try_again`; only an explicit domain retry does.
   Reviewer may veto.

---

## 11. References

- **ADR-085 §3.4** — the trigger-derived identity this ADR completes.
- **ADR-069 §4.6** — the saga-level timeout whose deterministic
  `event_id` is the framework's precedent (§5.1).
- **ADR-074** — the per-system cursor; the FSM's replay gate (§5.2).
- **ADR-034** — the `ToolCallRequest` / `ToolCallCompletion` slots
  and their join (§3.3, §5.5).
- **ADR-036 §4.3** — `request_event_id` as the join key.
- **ADR-045** — the Tool Call TTL; why the sweeper is not a retry
  mechanism (§3.4).
- **`core/result/errors.py:39-42`** — `ToolErrorClass`.
- **`tools/_message_handlers.py:288-298`** — the `.failed` event's
  `error_class`.
- **`tools/manager.py:622`** — the worker's `idempotency_key`.
- **`core/event/id_helpers.py`** — the two hash modes; the
  deterministic-identity idiom.
- **`core/event/correlation.py:57`** — the `correlation_id` is the
  flow id (why B is rejected).
- **`concordos/saga/_timeout_system.py:100-112`** — the Saga's
  deterministic `event_id` (§5.1).
- **`concordos/fsm/_system.py:149-157`** — the FSM's cursor gate
  (§5.2).
- **`agents/role_systems/_prompts.py:107-125`** — the unused
  `chat_idempotency_key` helper (§6.1).
