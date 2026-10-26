<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-085: A tool request has one identity; the reaper reclaims on the request's own deadline

- **Status:** Partially Accepted — §3.1–§3.3 shipped in commit `8d1fba2` (this ADR retrofits the missing written record); §3.4–§3.6 are **Proposed** and pending the §8 questions. §1.5.1 and §3.5 were revised on 2026-10-25 after a production measurement (the backoffice's live Redis).
- **Date:** 2026-10-24 (record retrofitted 2026-10-25; §1.5/§3.5 revised after production measurement)
- **Author:** kinetgraph architecture team (raised by the operator: *"as filas das tools que tem a execução demora estão tendo suas requisições de execução duplicadas fazendo a fila crescer"*)
- **Related to:**
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — §3.2 of this ADR reuses the three-phase `claim_event_id_slot` idempotency pattern unchanged.
  - [ADR-034](./ADR-034-ToolCall-ECS-Components.md) — the `ToolCallRequest` / `ToolCallCompletion` ECS slots. §3.4 explains why the request identity cannot live in the slot.
  - [ADR-036](./ADR-036-Tool-Worker-Pattern.md) — the Tool Worker Pattern. §2.5 names `ToolRouter.route_batch` as the fan-out point; §3.5 corrects ADR-083's premise about it.
  - [ADR-075](./ADR-075-Tool-Task-Observability-and-Recovery.md) — the recovery tier. §3.3 fulfils the non-idempotent-tool promise ADR-075 §3.5 made for the sweeper but never reached the reaper.
  - [ADR-083](./ADR-083-Tool-Router-Dedup-By-Event-Id.md) — the queue-boundary dedup. **§3.5 corrects its central premise**: the replays do *not* carry the same `event_id`, so the ADR-083 index cannot collapse them.
  - [ADR-077](./ADR-077-Storage-Adapter-Exception-Translation.md) — the narrow-catch discipline at the storage boundary. §3.2 keeps it on the new per-message read path.
  - [`kntgraph-typed-errors`](../.agents/skills/kntgraph-typed-errors/SKILL.md) §6.2 — mutating storage adapters return `Result[T, E]`. §3.2 follows the convention.
  - [`kntgraph-file-layout`](../.agents/skills/kntgraph-file-layout/SKILL.md) §3.3 — the public/private boundary. §3.4's derivation lives in the existing `tools/system.py` helper (`_stable_event_id_for_tool_request`); no new module is introduced.
  - [`kntgraph-testing`](../.agents/skills/kntgraph-testing/SKILL.md) §7.1 — behaviour tests over the real `World`, the real `ToolRouter`, and the real role system. §5 enumerates the coverage gate; no monkey-patch on a core class.
  - [`kntgraph-environment`](../.agents/skills/kntgraph-environment/SKILL.md) — `KNT_REDIS_FAKE=1` for the unit path.

---

## 1. Context

### 1.1 The symptom

The operator reports two symptoms that look unrelated but share one root.

**Symptom 1 — the queue grows.** The per-tool Redis Streams
(`knt:tools:<name>:queue`) accumulate entries faster than the worker
drains them. The same logical request appears many times.

**Symptom 2 — the request executes twice.** A slow tool is reclaimed
by the reaper while it is still running, so the same request is
executed concurrently by two workers.

### 1.2 The measured incident (backoffice, 2026-10-14)

The backoffice `document_text_extractor` tool hit a 5.5-hour latency
on 2026-10-14. The queue for that tool held **67 entries** and the
worker had `max_concurrency=1` with a ~5-minute per-item cost:

> 67 entries × ~5min = ~5.5 hours to drain the queue.

ADR-083 §1.2 records the operator's post-mortem. This ADR re-reads
that record and finds its premise wrong (§3.5) — the consequence is
that ADR-083's mitigation cannot prevent the incident it was written
for.

### 1.3 What the reaper does today, and why it reclaims a running tool

`XAUTOCLAIM` takes a `min_idle_time`. Redis computes the idle time
from the **last delivery**, and a message being processed right now
is in the PEL without an `XACK`. A tool slower than the threshold is
therefore reclaimed **while it still runs**.

The threshold was the global `reaper_idle_time` (default 300 s,
`tools/manager.py:104`). The tool's own latency budget was declared
elsewhere — `@tool_worker(timeout_seconds=...)` — but never reached
the router, so it never reached the payload, so the reaper never saw
it.

This ADR's §3.1–§3.3 close that gap. **They are already shipped**
(commit `8d1fba2`).

### 1.4 The gap this ADR adds: the request identity is not stable

The three shipped changes make the reaper reclaim on the *right*
deadline. They do **not** address why the queue holds 67 copies of
one request in the first place.

The event id is derived from the causation id
(`core/event/id_helpers.py`, `_legacy_event_id`):

```python
base = f"{causation_id}|{event_type}|{payload_str}"
```

The `causation_id` is therefore *an input to the identity*, not the
identity itself. The role system supplies that `causation_id` from
`view.last_event_id` (`agents/role_systems/_base.py`, `_emit_request`):

```python
last_eid = view.last_event_id
e = self.request_tool(
    ...
    causation_id=str(last_eid),
    ...
)
```

But the emitted request is a **domain** event, so folding it
**overwrites** `last_event_id` with the request's own id
(`core/world/projection.py`, `_apply_event`):

```python
last_event_id=str(event.event_id),
```

The loop is therefore self-sustaining:

```
tick 1: causation_id = intent     -> request id R1
fold R1: last_event_id = R1
tick 2: causation_id = R1         -> request id R2  (R2 != R1)
fold R2: last_event_id = R2
tick 3: causation_id = R2         -> request id R3  (R3 != R2)
```

Each wake produces a **new** request with a **new** `event_id`.
Verified against the real role system:

```
tick1 request event_id : 35480f36-4398-58e1-9dff-55f9f7c0fa15
re-emit caused-by-request: 81229fed-0b5e-5f1c-8894-0d7d948fcf23
SAME as tick1?         : False
```

The request *content* is identical across the ticks — only the
`causation_id` moved. These are two identities for one logical
request.

### 1.5 Why every existing dedup mechanism misses it

Both dedup indices are keyed on `event_id`:

| Index | Key | Source |
| --- | --- | --- |
| EventLog dedup | `knt:eventids:<event_id>` | `infra/redis/_event_log/_idempotency.py` |
| Tool-queue dedup (ADR-083) | `knt:tools:<tool>:idem:<event_id>` | `infra/redis/_tools/_idempotency.py` |

A changed `event_id` defeats both. Measured on the real
`ToolRouter` against `fakeredis`:

| Scenario | Queue length |
| --- | --- |
| `dedup=False`, same `event_id` routed twice | 2 |
| `dedup=True`, same `event_id` routed twice | **1** (dedup works) |
| `dedup=True`, **distinct** `event_id`s (the re-emit) | **2** (dedup does not help) |

The third row is the incident shape — **when the re-emit changes the
`event_id`**.

#### 1.5.1 Which shape is the incident? (measured in production)

The 2026-10-14 backoffice incident was measured against the live
Redis on 2026-10-25. The answer is **both**, and the two halves need
different fixes:

| The re-emitter | `idempotency_key`? | `event_id` stable? | ADR-083 dedup helps? |
| --- | --- | --- | --- |
| `agents/role_systems/_base.py` (framework role systems) | ❌ none | ❌ unstable | **no** |
| The backoffice's `document_parser` / `document_text_extractor` | ✅ stable | ✅ stable | **yes** — the incident's 67 entries collapsed |
| The backoffice's `document_rasterizer` / `document_ocr_*` | ❌ none | ❌ unstable | **no** |

The backoffice's ADR-013 §1.1 records "67 entries with the **same**
`event_id`" — and its own §1.2 confirms the EventLog held **one**
entry. Those two statements are only consistent if the `event_id`
was **stable** across the replays, i.e. the re-emitter passed a
stable `idempotency_key`. It did, and ADR-083's dedup **did**
resolve that backlog.

So the shape that ADR-083 addresses is real, and its mitigation
works **for a re-emitter that passes an `idempotency_key`**. The
shape it does not address is the re-emitter that does not — which
is the framework's own role systems and two of the backoffice's
request paths.

**This ADR's §3.4 therefore closes the gap for the re-emitters that
omit the `idempotency_key`, and §3.5's correction of ADR-083 is
narrowed accordingly** (see §3.5.1).

### 1.6 The framework already has the fix — it is not connected

`core/event/id_helpers.py` has two hash modes:

```python
if idempotency_key is not None:
    return _idempotent_event_id(idempotency_key, agent_id, event_type, data)
return _legacy_event_id(causation_id, agent_id, event_type, data)
```

`_idempotent_event_id` **excludes** the `causation_id` from the hash,
so the same logical request collapses to one id no matter how many
times it is re-emitted:

```python
base = f"ik:{idempotency_key}|{agent_id or ''}|{event_type}|{payload_str}"
```

`ToolAwareSystem.request_tool` accepts the kwarg
(`tools/system.py`, `idempotency_key: str | None = None`). **No
production call site passes it.** The only framework caller of
`request_tool` is `agents/role_systems/_base.py`, and it passes only
`causation_id`.

The convention is already declared in `agents/__init__.py`:

> **idempotency_key**: toda Tool recebe `idempotency_key=str(request.event_id)`.
> Roles devem repassar essa chave (ou construir uma estável) ao chamar a Tool.

The code and the declared convention disagree. The code is wrong.

### 1.7 The declaration split

The tool's latency budget had two homes: the tool's own
`@tool_worker(timeout_seconds=...)` and the dispatcher's
`ToolCallTTL.per_tool_ttls`. A divergence between them re-opened the
duplicate execution. The declaration belongs with the tool that has
the latency.

### 1.8 The missing ADR file

Commit `8d1fba2` shipped the §3.1–§3.3 changes and cited "ADR-085"
in **33 places** across `src/` and `tests/`, but the ADR file was
never written. The written record of the decision — the alternatives
rejected, the rationale for the per-message threshold, the reason
the tool owns its timeout — exists only in a commit message. This
ADR retrofits that record.

---

## 2. The two problems, and what they share

| | Problem A: the queue grows | Problem B: the request executes twice |
| --- | --- | --- |
| Mechanism | The re-emit mints a new `event_id` per wake; each one is a new queue entry | The reaper's threshold is shorter than the tool's real latency |
| Layer | Request identity (`core/event`, `agents/role_systems`) | Reclaim policy (`tools/_reaper.py`, `tools/_ttl.py`) |
| Amplifier | A worker that cannot keep up (`max_concurrency=1`) | A tool slower than the global `reaper_idle_time` |
| Mitigation available | None today — `dedup=True` does not help (§1.5) | Shipped in `8d1fba2` (§3.1–§3.3) |

**What they share:** both are *the framework's* contracts, not a
deployment's configuration. §3.1–§3.3 fix Problem B in the framework
(`tools/`, framework tier). §3.4–§3.6 fix Problem A in the framework
too — the request identity is derived by `core/event/id_helpers.py`
and `tools/system.py`, both framework tier. The consumer that
*triggers* the re-emit (`agents/role_systems`) is a vertical, but it
is a consumer of a framework contract, and the defect is in the
contract's default.

---

## 3. The decision

### 3.1 The reclaim threshold is per message (shipped in `8d1fba2`)

The request carries its remaining TTL, so a slow tool is not
reclaimed early. `reaper_idle_time` is the fallback for a payload
with no TTL.

`XAUTOCLAIM` takes one `min_idle_time` per batch, but the TTL lives
in the payload — which only returns **with** the claim. The reaper
therefore reads the PEL (`XPENDING`) for the idle time and the stream
(`XRANGE`) for the payload, then claims each message individually. A
batch claim would take ownership of messages that are not ready to be
reclaimed — the duplicate execution this ADR exists to prevent.

Implemented by `tools/_ttl.py` (`read_ttl_remaining`) and
`tools/_reaper.py` (`reclaim_threshold_ms`).

### 3.2 The tool owns its timeout (shipped in `8d1fba2`; reworked in the issue-83 change)

`@tool_worker(timeout_seconds=...)` declares it and
`WorkerManager.register()` records it (`tools/manager.py`,
`_published_timeouts`). The reaper — which runs inside the manager,
and therefore already holds the declaration — derives the deadline
from it and the request's own timestamp:

```
requested_at + timeout − now
```

`requested_at` is the request event's `timestamp`, which travels in
the queued payload (`tools/_ttl.py`, `read_requested_at`).

**The deadline is derived, not published.** The first cut of this
section had the router write a pre-computed `ttl_remaining` into the
request's `data`, which required a publication step that nothing
called — so the declared timeout was recorded and then silently
ignored (issue #83). Deriving the deadline removes the missing link:
there is nothing to publish, so there is no wiring to get wrong. It
also removes two defects the pre-computed TTL had: the value went
stale while the request waited in the queue, and it spread the
decision about execution time into the router — the lower-level
component that only writes a request to a queue.

The same pattern the reaper already used for `idempotent`: a declared
property read straight off the registered tool.

A missing or unusable timestamp returns `None`; the caller falls
back to the global `reaper_idle_time`. The reader never invents a
value and never raises: a reader that raises would take the reaper
down; one that guesses would reclaim a message early.

### 3.3 A non-idempotent tool is never re-executed (shipped in `8d1fba2`)

The reaper resolves a non-idempotent request with a permanent failure
instead of re-running it. ADR-075 §3.5 promised this for the sweeper;
the reaper never had it.

### 3.4 A request's identity is derived from the request, not from its cause *(new)*

> **A re-emitted tool request MUST produce the same `event_id` as the
> original, regardless of how many times the emitting system re-emits
> it. The framework derives that identity; it is not the caller's
> responsibility to remember it.**

The derivation lives in the framework, in the two places that already
own the concern:

**`core/event/id_helpers.py`** — the hash mode. The
`_idempotent_event_id` mode already excludes the `causation_id`. It
becomes the mode for tool requests.

**`tools/system.py`** — `ToolAwareSystem.request_tool`. The
`_stable_event_id_for_tool_request` helper already computes an
explicit `event_id` when `idempotency_key` is passed. §3.4 changes
the **default**: when the caller does not supply an `idempotency_key`,
the helper derives one from the request's own stable content rather
than returning `None` and deferring to the causation-based hash.

The derivation must be **stable across re-emits** and **distinct
across distinct requests**. The stable content is the *trigger* of the
request — the intent, the plan, the summary request — not the
self-referential `view.last_event_id` that the request itself
overwrites.

The trigger is available to the caller as the ECS component named by
the system's `REQUEST_EVENT_TYPE` (e.g. `user.intent`), and — for
the role systems — as `SessionComponent.intent_event_id`. The
derivation therefore needs the *trigger's* id, which the caller
already knows.

The API keeps the explicit `idempotency_key=` kwarg: a caller that
knows better (a vertical with its own request identity) must be able
to say so.

**Why not put the identity in the ECS slot?** `ToolCallRequest` is
keyed by `request_event_id` (`core/world/components.py`). The slot
records what was requested; it does not mint the id. An identity
that depends on a slot is circular — the slot's key *is* the
`event_id`.

**Why not change the default hash mode globally?** `_legacy_event_id`
is the framework-wide default for every event type. Changing it
globally would silently re-key every historical event id in every
deployment's EventLog — the dedup index `knt:eventids:<id>` would
no longer match, and replayed events would append twice. The change
is scoped to the tool-request path, where the trigger is known.

### 3.5 The queue-boundary dedup is a safety net, not the fix *(new — corrects ADR-083)*

ADR-083 §1.1 states:

> The replays in the backoffice incident come from the SAME dispatcher
> process re-emitting the SAME `Event` (carrying the SAME `event_id`)
> on each wake.

**This is correct for the incident it describes.** The measurement in
§1.5.1 confirms it: the backoffice's re-emitter passed a stable
`idempotency_key`, so the replays carried the same `event_id`, and
ADR-083's index collapsed them.

**An earlier draft of this ADR claimed the opposite** — that the
replays carried *different* `event_id`s and that ADR-083's
mitigation "cannot prevent the incident it was written for". That
claim was generalised from the framework's own role systems (which
pass no `idempotency_key`) to the backoffice (which does). The
production measurement refutes it, and **ADR-083 stands**.

What §3.4 adds is coverage for the re-emitters that ADR-083 cannot
help: the ones whose `event_id` genuinely changes. That is the
framework's role systems and the backoffice's rasterizer/OCR paths.

This ADR does not revert ADR-083. The dedup index remains the guard
against a repeated `XADD` of the same event, and it costs one `SET`
per `XADD`.

#### 3.5.1 A hypothesis that was tried and disproved

Enabling the dedup by default (`dedup=True` as the `ToolRouter`
default) was attempted. The premise was measured first — against
both the re-emitter that passes an `idempotency_key` and the one that
does not:

| Scenario | Queue length |
| --- | --- |
| `dedup=False`, same `event_id` routed twice | 2 |
| `dedup=True`, same `event_id` routed twice | **1** — the dedup works |
| `dedup=True`, **distinct** `event_id`s (the re-emit that omits the key) | **2** — the dedup does not help |

The first two rows are the **production incident**, and they show the
dedup works. The third row is the gap §3.4 closes.

Enabling the dedup by default therefore *does* prevent the incident
for every re-emitter that passes an `idempotency_key` — which is the
majority of a well-behaved deployment — at the cost of one extra
`SET` per `XADD`. The change was reverted in this cycle because the
**cost/benefit is a deployment decision**, and because the
backoffice — the deployment that motivated it — already opts in
explicitly (`dedup=True` in its `ToolRouter` construction).

**The generalisable lesson:** a dedup index keyed on `event_id`
cannot help when the defect is that `event_id` changes. The two
failure modes must be distinguished before choosing a mitigation —
and the way to distinguish them is to measure, not to infer from the
post-mortem's prose.

#### 3.5.2 A second defect the production measurement exposed

The measurement also found a defect **inside the framework** that is
unrelated to the re-emit and was not previously documented: the
`ToolCallTTLSweeperSystem` emits a **new** `tool.<name>.failed`
event for the same expired request on every tick, because
`swept_at` (a wall-clock timestamp) is part of the event's `data` and
therefore part of its `event_id` hash. The EventLog cannot dedup
what it sees as four distinct events.

Measured in production: three `tool.backend_event_publisher.failed`
events with the same `request_event_id`, the same `expired_at`, and
three different `event_id`s — repeated in two agents (six
duplicates in total).

That is a **separate** defect from the re-emit, and it is tracked in
the backoffice's diagnostic
(`soldi/backoffice/docs/features/012-diagnostico-duplicacao-execucao-tools.md`).
It is recorded here because it is the same class of bug — a
non-deterministic input in the identity hash — and because the
framework's TTL sweeper is framework tier, so the fix belongs
upstream.

**Resolved 2026-10-25:** [ADR-087](./ADR-087-TTL-Sweeper-Stable-Failure-Identity.md)
was written and the fix shipped. The sweeper's failure event now
derives its `event_id` from the request's stable properties only.

### 3.6 The re-emit is also a dispatcher-level defect *(new)*

Even with a stable identity, the role system re-emits on every wake.
That is wasteful: it produces an event, appends it, and routes it,
only for the identity layer to collapse it.

The re-emit is gated by `_is_new_event` (`agents/role_systems/_base.py`),
which compares `view.last_event_id` against the previous one. Because
the request overwrites `last_event_id`, the gate re-arms itself.

The gate should compare against the **trigger** the system already
tracks — the same anchor §3.4 uses for the identity — so a system
that has already requested for that trigger does not request again.

This is a secondary optimisation: with §3.4 in place the queue is
correct regardless. It is recorded here because the two changes
share the anchor, and shipping them together avoids two rounds of
identity plumbing.

---

## 4. Configuration surface

| Symbol | Where | Default | Override |
| --- | --- | --- | --- |
| `idempotency_key` on `request_tool` | `tools/system.py` kwarg | derived (§3.4) | per-call |
| `dedup: bool` | `ToolRouter.__init__` kwarg | `False` | per-router (ADR-083) |
| `dedup_ttl_seconds: int` | `ToolRouter.__init__` kwarg | `3_600` | per-router (ADR-083) |
| `timeout_seconds: float` | `@tool_worker(...)` | `None` | per-tool (§3.2) |
| `reaper_idle_time: float` | `WorkerManager.__init__` | `300.0` | per-manager (fallback only) |
| `reaper_interval: float` | `WorkerManager.__init__` | `60.0` | per-manager |

No new Settings field and no new env var: every knob above already
exists. §3.4 changes a **default**, not a surface.

---

## 5. Tests

### 5.1 The regression test that pins Problem A

`tests/unit/tools/test_tool_request_duplication.py` (13 tests, no
monkey-patch on a core class — the `kntgraph-testing` §7.4 rule):

1. **Identity is not stable across wakes** — the re-emit produces a
   different `event_id`; the `causation_id` moves to the previous
   request; the payload is identical.
2. **The queue grows** — two wakes produce two entries; five wakes
   produce five; both entries describe the same work.
3. **`dedup=True` does not fix the re-emit** — the ADR-083 index
   collapses the same `event_id` (1 entry) but not the re-emit (2
   entries). This is the finding that corrects ADR-083 §1.1.
4. **The stable identity is reachable** — `idempotency_key` makes the
   `event_id` independent of the `causation_id`; `request_tool` with
   the kwarg is stable across the re-emit; and the end-to-end effect
   is a queue that stays at one entry.

The test detects the fix: applying a candidate §3.4 derivation turns
4 of the 13 red (the identity and the queue-growth assertions),
which is what makes it a regression test rather than a description of
current behaviour.

### 5.2 The shipped changes (already covered)

- `tests/unit/tools/test_reaper_timeouts.py` — the per-message
  threshold, the non-idempotent resolution, the in-flight guard.
- `tests/unit/tools/test_register_publishes_timeout.py` — the
  publication contract: `register()` publishes, the router writes it.
- `tests/unit/tools/test_ttl.py` — the reader's three decisions.
- `tests/unit/core/test_clock.py` — the framework clock (ADR-085 E).
- `tests/unit/tools/test_router_ttl.py` — the payload wire format.

### 5.3 Coverage for §3.4 and §3.6 (to be written with the change)

- The derivation is stable across re-emits for every role system.
- The derivation is distinct across distinct triggers (two intents in
  the same session produce two requests).
- An explicit `idempotency_key=` overrides the derivation.
- The re-emit gate does not re-request for an already-requested
  trigger.

---

## 6. Alternatives considered

| Alternative | Pros | Cons | Verdict |
| --- | --- | --- | --- |
| **A. Enable the queue dedup by default (`dedup=True`) and call it fixed** | One line; ADR-083 already exists | Does **not** prevent the incident (§1.5, §3.5.1) — the `event_id` changes; costs one `SET` per `XADD` on every request | **Rejected** — it fixes a symptom that is not the mechanism, and it makes the deployment *look* protected while the queue grows |
| **A2. Dedupe in the worker by content hash (`agent_id` + tool + params)** | The worker already holds the full request payload (§1.5); no new index; the duplicate is detected where it would otherwise be executed | Collapses two *legitimately identical* requests (the same tool, same params, twice in the same flow) into one — the framework cannot distinguish them without the trigger's identity | **Open** — §8 Q2; viable only if the framework can tell an accidental replay from an intentional repeat |
| **B. Let the vertical pass `idempotency_key`** | No framework change | Makes a framework contract the deployment's problem; the vertical cannot know the trigger's id without duplicating the anchor; ADR-083 §7 shows the "flip a flag" rollout does not work | **Rejected** — the contract is the framework's (§2) |
| **C. Change the default hash mode globally** | One line | Re-keys every historical event id in every deployment; the EventLog dedup index stops matching; replayed events append twice | **Rejected** — too broad |
| **D. Put the request identity in the `ToolCallRequest` ECS slot** | The slot already exists | Circular — the slot's key *is* the `event_id` | **Rejected** |
| **E. Make `tool.<name>.requested` a lifecycle event** | Lifecycle events do not overwrite `last_event_id` | Breaks the ADR-036 canonical form; a tool request *is* domain state | **Rejected** |
| **F. Dedupe at the dispatcher (`ReactiveDispatcher`) before emitting** | Stops the re-emit early | Conflates concerns with the EventLog dedup; does not survive a second dispatcher instance; ADR-083 §1.3 rejected the same shape | **Rejected** — §3.6 fixes the gate at the system that re-emits |
| **G. Dedupe in the worker (`WorkerManager`) before invoking** | Saves the invocation | The queue still grows; the `XLEN` alert still fires; the backlog still blocks the real request | **Rejected** — ADR-083 §1.3 rejected the same shape |
| **H. Keep the request as a domain event but stop the request from overwriting `last_event_id`** | Breaks the loop without touching the hash | `last_event_id` is the projection's general pointer; special-casing one event type there is a framework-wide special case | **Rejected** — §3.4 fixes the identity, which is where the defect actually is |
| **I. (this ADR) Derive the request identity in the framework from the request's trigger; keep the queue dedup as a safety net; gate the re-emit on the same anchor** | Fixes the cause; scoped to the tool-request path; leaves historical event ids intact; keeps the explicit kwarg for callers that know better; the queue-boundary dedup survives as a cheap guard | Touches `core/event`, `tools/system.py`, and the role systems; changes a default | **Recommended** |

---

## 7. Migration path

1. **§3.1–§3.3 ship** (already in `8d1fba2`). The reaper reclaims on
   the request's own deadline.
2. **§3.4 lands** with the regression test of §5.1 turning green. The
   request identity becomes stable; the queue stops growing.
3. **§3.6 lands** with §3.4 (shared anchor). The re-emit stops
   producing redundant events.
4. **§3.5 lands** as a documentation correction: ADR-083 §1.1 and
   §7 are annotated as superseded, so the next operator does not
   "fix" the backoffice by flipping `dedup=True`.
5. **`dedup=True` stays opt-in.** It remains the guard against a
   concurrent double-write of the same event. The attempt to enable it
   by default was measured and reverted (§3.5.1).

### 7.1 Scope boundary — what belongs to which change

The two problems are fixed by different changes, on different tiers,
and the branch boundary follows that:

| Change | Tier | Files | Lands with |
| --- | --- | --- | --- |
| §3.1–§3.3 (reaper deadline) | framework (`tools/`) | `_reaper.py`, `_ttl.py`, `manager.py`, `worker.py` | `8d1fba2` — **already shipped** |
| §3.4–§3.6 (request identity) | framework (`core/event`, `tools/`) + vertical consumer (`agents/role_systems`) | `id_helpers.py`, `system.py`, `_base.py` | a separate change, after §8 is resolved |
| §3.5 (documentation correction) | ADR only | `ADR-083` | with the §3.4 change |

§3.1–§3.3 are independent of §3.4: the reaper reclaims on the
right deadline whether or not the request identity is stable. The
identity change is therefore **not** a prerequisite for the reaper
fix, and the reaper fix is **not** a prerequisite for the identity
change. They can ship independently, in either order.

---

## 8. Open questions for review

1. **Derivation input.** §3.4 anchors the identity on the request's
   *trigger*. The trigger's id is available to the role systems via
   `SessionComponent.intent_event_id` and via the
   `REQUEST_EVENT_TYPE` component. Which is canonical? The session
   anchor is stable across the request's whole life; the component is
   stable only while the component is the last domain event.
   **Recommendation:** the session anchor when present, the component
   otherwise. Reviewer may veto.
2. **Scope of the §3.4 default.** Should the derived identity apply to
   *every* `ToolAwareSystem` consumer, or only to the role systems?
   `agents/memory/solution_lookup.py` is also a `ToolAwareSystem`
   consumer. **Recommendation:** the framework derivation lives in
   `tools/system.py` and therefore applies to every consumer; a
   consumer that needs a different anchor passes `idempotency_key=`
   explicitly.
3. **Does §3.6 need a separate ADR?** It changes the role systems'
   re-emit gate, which is ADR-074's per-system cursor territory.
   **Recommendation:** keep it in this ADR (shared anchor) and
   cross-reference ADR-074; split only if the cursor interaction
   proves more involved than expected.
4. **ADR-085's missing file.** This ADR retrofits the record for
   commit `8d1fba2`. Should the shipped §3.1–§3.3 changes be
   re-labelled to point at §3.1–§3.3 of this ADR? The code already
   cites "ADR-085" in 33 places, which now resolve. Reviewer may veto.
5. **The TTL sweeper's unstable identity (§3.5.2).** The production
   measurement found the framework's own `ToolCallTTLSweeperSystem`
   emitting a distinct `tool.<name>.failed` per tick for the same
   expired request, because `swept_at` is part of the event's `data`
   and therefore part of its `event_id` hash. The fix is upstream
   and framework tier. Should it be:
   - **(a)** fixed here, as a new §3.7 — it is the same class of
     defect (a non-deterministic input in the identity hash) and the
     same tier, but a different subsystem from the reaper;
   - **(b)** a separate ADR (ADR-087), since it is the sweeper's
     identity rather than the request's identity;
   - **(c)** handled by the vertical — rejected: the sweeper is
     framework tier, and the backoffice cannot fix the framework's
     event identity.
   **Recommendation:** (b). The defect is real, measured, and
   upstream; it deserves its own record with its own acceptance
   criteria rather than being folded into a decision about the
   request identity. Reviewer decides.
   **Resolved 2026-10-25:** (b) — [ADR-087](./ADR-087-TTL-Sweeper-Stable-Failure-Identity.md)
   was written and the fix shipped. The sweeper's failure event now
   derives its `event_id` from the request's stable properties only;
   the EventLog collapses the duplicates (verified end-to-end: three
   ticks now produce one stream entry instead of three).
6. **Should `publish_timeouts()` get a production call site?**
   §3.2 makes the tool's declared `timeout_seconds` reach the
   queued request, but `tools/router.py`'s `publish_timeouts()` has
   **no production caller** — the backoffice declares no
   `timeout_seconds` at all, and even if it did, nothing would
   publish it. The wiring (register the tools, then publish) is
   currently the operator's job and is not documented anywhere.
   **Recommendation:** the framework should publish the timeouts
   itself — either the `WorkerManager` pushes to the router, or the
   `ReactiveDispatcher` wires the two when it owns both. Otherwise
   §3.2 is inert in practice, and the 300 s fallback silently
   reclaims tools that legitimately take longer. Reviewer decides.
   **Resolved 2026-10-26:** the framework no longer publishes
   anything, because nothing needs publishing (issue #83). The
   reaper derives the deadline from the tool's declared timeout and
   the request's own timestamp — the same pattern it already used
   for `idempotent`. `ToolRouter.publish_timeouts()` and the
   pre-computed `ttl_remaining` are gone; the router is blind to
   execution time. §3.2 is no longer inert, and there is no wiring
   for an operator to get wrong.
5. **Content-hash dedup in the worker (§6, alternative A2).** The
   operator's proposal: the worker already holds the full request
   payload, so it could detect the duplicate **at consumption time**
   by hashing `(agent_id, tool, params)` — no new index, no change to
   the event identity, and the duplicate is caught exactly where it
   would otherwise be executed. The open problem is the false
   positive: two *legitimately identical* requests (the same tool,
   the same params, twice in the same flow) hash the same. The
   question is whether the framework can tell an accidental replay
   from an intentional repeat without the trigger's identity — for
   example by consulting the EventLog for a **prior completion** of
   the same `(tool, params)` within the flow, which is the join the
   `ToolCallCompletion` slot already models.
   **Recommendation:** evaluate A2 before committing to §3.4. If the
   EventLog's completion history can disambiguate, A2 is
   substantially simpler: it changes no event identity, breaks no
   historical index, and puts the dedup where the operator expects
   it. If it cannot, §3.4 is the fix and A2 is a second line of
   defence. Reviewer decides.

---

## 9. References

- **Backoffice post-mortem, 2026-10-14** — quoted in ADR-083 §1.2.
- **ADR-019 §3.2** — the three-phase idempotency pattern this ADR's
  §3.2 reuses.
- **ADR-075 §3.5** — the non-idempotent-tool promise §3.3 fulfils.
- **ADR-083 §1.1, §7** — the premise and rollout plan §3.5 corrects.
- **`src/kntgraph/core/event/id_helpers.py`** — the two hash modes
  §3.4 selects between.
- **`src/kntgraph/tools/system.py`** — `ToolAwareSystem.request_tool`,
  the derivation site §3.4 changes.
- **`src/kntgraph/agents/role_systems/_base.py`** — `_emit_request`
  (§1.4) and `_is_new_event` (§3.6).
- **`src/kntgraph/core/world/projection.py`** — `_apply_event`, where
  the request overwrites `last_event_id` (§1.4).
- **`src/kntgraph/tools/_reaper.py`**, **`src/kntgraph/tools/_ttl.py`** —
  the shipped §3.1–§3.3.
- **`tests/unit/tools/test_tool_request_duplication.py`** — the
  regression test of §5.1.
