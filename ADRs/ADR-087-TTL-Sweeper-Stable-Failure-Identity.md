<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-087: The TTL sweeper's failure event carries a stable identity

- **Status:** Accepted
- **Date:** 2026-10-25
- **Author:** kinetgraph architecture team (found by a production measurement of the `soldi/backoffice` Redis on 2026-10-25)
- **Related to:**
  - [ADR-045](./ADR-045-Tool-Call-Request-TTL.md) — the Tool Call TTL. §1.1 describes the sweeper this ADR hardens.
  - [ADR-044](./ADR-044-Tool-call-Overlay-Accumulation.md) — the completion-driven eviction the sweeper is the safety net for.
  - [ADR-075](./ADR-075-Tool-Task-Observability-and-Recovery.md) — the recovery tier. §3.2 explains why the duplicate failure was worse than a duplicate log line.
  - [ADR-085](./ADR-085-Tool-Request-Identity-and-Reaper-Deadline.md) — §3.5.2 of that ADR records this defect and §8 Q5 asked whether it belongs there; this ADR is the answer.
  - [ADR-019](./ADR-019-Redis-Adapter-Typing.md) — the EventLog's three-phase `claim_event_id_slot` idempotency pattern, whose guarantee this ADR restores.
  - [`kntgraph-testing`](../.agents/skills/kntgraph-testing/SKILL.md) §7.1 — behaviour tests over the real `EventLog`. §5 pins the end-to-end collapse.
  - [`kntgraph-type-discipline`](../.agents/skills/kntgraph-type-discipline/SKILL.md) §3.1 — `JsonValue` for wire payloads. The split between the payload and the identity inputs uses it.

---

## 1. Context

### 1.1 The sweeper

`ToolCallTTLSweeperSystem` (`runner/tool_call_ttl_sweeper.py`) is a
`WorldSystem` that runs once per dispatcher tick. It walks the
`tool_requests` slot of every agent's view and emits a
`tool.<name>.failed` event for every request whose `expires_at` is
in the past — the safety net for a request whose completion never
lands (ADR-045 §2, ADR-044 §2.3).

### 1.2 The defect

The emitted event's `data` carried `swept_at` — the wall-clock time
of *that* sweep:

```python
data={
    "error": self._error_message,
    "request_event_id": request.request_event_id,
    "tool_name": tool_name,
    "expired_at": request.expires_at.isoformat(),
    "swept_at": now.isoformat(),          # <-- wall clock, changes every tick
}
```

An `Event`'s `event_id` is derived from its `data` when no explicit
id is given (`core/event/id_helpers.py`, `_legacy_event_id`):

```python
base = f"{causation_id}|{event_type}|{payload_str}"
```

Since `swept_at` changes on every tick, **every tick minted a new
`event_id` for the same expired request**.

### 1.3 Why that breaks the guarantee

The sweeper's own docstring states the dedup contract
(`runner/tool_call_ttl_sweeper.py:146-157`):

> The system is **stateful** (`_emitted_failures`): it remembers the
> `request_event_id`s for which it has already emitted a `failed`
> event, so a request that stays in the slot across multiple ticks
> (because the completion never arrives) triggers **at most one**
> failed event. The dedup is in-memory; a process restart re-derives
> the dedup from the EventLog via the `causation_id` on subsequent
> events.

The second sentence is the load-bearing one, and it **did not
hold**. The EventLog dedupes on `event_id` (`infra/redis/_event_log/_idempotency.py`,
`claim_event_id_slot`), not on `causation_id`. With an unstable
`event_id`:

- the EventLog stored each sweep as a **distinct** event;
- the re-derivation after a process restart produced yet another
  distinct event;
- the in-memory `_emitted_failures` set is per-instance and per-process.

So the "at most one failed event" contract held only while a single
process was alive — and even then only because the set short-circuited
before the event was built. A restart, or a second dispatcher
process, re-emitted.

### 1.4 Measured in production

Collected from the `soldi/backoffice` Redis (`192.168.0.9:6379`) on
2026-10-25. In the agent stream
`soldi:backoffice:knt:agents:364b8d8c-3cfc-4a99-a3df-3f31627cee10:events`,
**three** `tool.backend_event_publisher.failed` events carry:

- the **same** `request_event_id` = `02fb943c-fed8-519f-806b-fc9e7b7f738e`
- the **same** `expired_at` = `2026-10-23T21:16:00.213031+00:00`
- the **same** `error` = `ttl_expired`
- **three different `event_id`s** — so the EventLog did not collapse them

```
event_id                                 swept_at
23ec0e5b-dbff-5197-8aa4-e13d973f6287     ...21:16:00.460021
aa375f6b-3fa2-59ca-9fd1-37045e733421     ...21:16:00.460089
52ed7075-504d-56e7-89f1-71b20a16c901     ...21:16:00.460152
```

The same pattern repeats in a second agent
(`f63fe5c4-4ea5-4fa6-8298-910821dd4aec`) — six duplicates in total.

The EventLog's idempotency index corroborates it: only **two**
entries in `soldi:backoffice:knt:eventids:*` for **nine** events in
that agent's stream. The duplicated failures never reached the index.

Reproduced in isolation:

```
swept_at=21:16:00.460021 -> event_id=71ab8558-7315-511b-9611-aa18e738a3aa
swept_at=21:16:00.460089 -> event_id=b5a7d830-5e75-5e32-9df4-96f07e915e90
swept_at=21:16:00.460152 -> event_id=7954800a-e36d-59c8-b7e7-3e9a4f571b8e
all distinct? True

without swept_at -> stable? True
```

### 1.5 Why it matters beyond a noisy log

ADR-075 §2.3 routes the sweeper's failure to the **DLQ** when a DLQ
is wired and the tool is non-idempotent. A duplicated failure event
therefore means a duplicated DLQ insertion, and the saga path
(§2.3.3) emits `saga.<name>.compensation_failed` from the same
signal — a duplicated failure can trigger a duplicated compensation.

The sweeper's own comment on the completion check names the hazard
(`runner/tool_call_ttl_sweeper.py:247-250`):

> the sweeper must NOT emit a duplicate failure (which would
> double-fire downstream compensation paths — the saga system
> reacts to both `completed` and `failed` events for the same
> `request_event_id`).

The in-memory guard prevented that *within* a process. The unstable
identity defeated it across processes.

---

## 2. The decision

> **The sweeper's failure event MUST carry an `event_id` derived
> only from the request's stable properties. A wall-clock reading
> of the sweep may appear in the event's payload, but never in its
> identity.**

The identity inputs are the request-derived fields:

| Field | Stable? | Why |
| --- | --- | --- |
| `error` | ✅ | the operator's configured message |
| `request_event_id` | ✅ | the request being expired |
| `tool_name` | ✅ | the request's tool |
| `expired_at` | ✅ | `requested_at + ttl` — fixed at materialisation (ADR-045 §2.1) |
| `swept_at` | ❌ | the wall clock of *this* sweep — **excluded** |

`swept_at` remains in the payload: it is useful audit information
("when did the sweeper notice this?"). Only the identity excludes it.

---

## 3. The implementation

### 3.1 The change

`runner/tool_call_ttl_sweeper.py` — the payload is unchanged; the
`event_id` is derived explicitly from the request's stable
properties:

```python
data: dict[str, JsonValue] = {
    "error": self._error_message,
    "request_event_id": request.request_event_id,
    "tool_name": tool_name,
    "expired_at": request.expires_at.isoformat(),
    _SWEPT_AT_KEY: now.isoformat(),
}
return Event.create(
    event_id=_stable_failure_event_id(
        agent_id=agent_id,
        event_type=event_type,
        request=request,
        data=data,
    ),
    ...
)
```

and the derivation, in a module-level helper (extracted so
`_build_failed_event` keeps its baseline cyclomatic complexity —
the ADR-019 complexity gate):

```python
def _stable_failure_event_id(
    *,
    agent_id: str,
    event_type: str,
    request: ToolCallRequest,
    data: Mapping[str, JsonValue],
) -> UUID:
    identity_data = {key: value for key, value in data.items() if key != _SWEPT_AT_KEY}
    return generate_deterministic_event_id(
        causation_id=request.request_event_id,
        agent_id=agent_id,
        event_type=event_type,
        data=identity_data,
    )
```

The excluded key is named explicitly (`_SWEPT_AT_KEY`) rather than
derived from a rule ("anything that looks like a timestamp"), so a
future field cannot silently destabilise the identity.

### 3.2 What did NOT change

- **The wire format.** The payload is byte-for-byte the same; the
  `event_id` is the only difference. A consumer reading
  `data["swept_at"]` is unaffected.
- **The `_emitted_failures` set.** It remains — it is a cheap
  short-circuit that avoids building the event at all. It is now an
  **optimisation**, not the guarantee. The guarantee is the EventLog's
  `event_id` dedup, which holds across processes and restarts.
- **The sweeper's semantics.** It still emits one failure per
  expired request; it still does not evict the request from the slot
  (ADR-044 §2.3).

### 3.3 The restored invariant

> One expired request produces exactly one `tool.<name>.failed`
> event, regardless of how many ticks observed it, how many
> dispatcher processes ran, or how many times the process restarted.

---

## 4. The generalisable rule

> **A derived identity must be a function of the thing it
> identifies, not of the observation.**

The sweeper observed the *same request* at three different moments
and produced three different identities for it. Any identity that
includes a wall-clock reading, a counter, a random value, or a
process-local token has this defect.

The framework already has the counter-example: the Saga's timeout
event derives its identity from the saga's content
(`concordos/saga/_timeout_system.py:107-112`), and the FSM gates on
a cursor (`concordos/fsm/_system.py:155-157`). Both are stable
because they identify the *subject*, not the *observation*.

This ADR is the third instance of the same rule, and the first one
where the violation was **measured** rather than inferred.

---

## 5. Tests

`tests/unit/runner/test_tool_call_ttl_sweeper.py`, class
`TestSweeperEventIdentity` (5 tests):

1. **`test_event_id_is_stable_across_ticks`** — two sweeper
   instances at different wall clocks emit the same `event_id`.
   This is the process-restart case, and the one the production
   measurement exposed.
2. **`test_event_id_is_stable_within_one_instance`** — the clock
   moving forward does not change the identity.
3. **`test_swept_at_still_reports_the_sweep_time`** — the fix must
   not drop `swept_at` from the payload. The audit information
   survives.
4. **`test_distinct_requests_get_distinct_event_ids`** — the
   stability must not collapse two different requests into one
   identity.
5. **`test_same_request_in_two_agents_shares_the_identity`** — the
   identity is a function of the request, not of the agent's sweep.

The end-to-end collapse (three ticks → **one** EventLog entry) is
verified against the real `EventLog` over `fakeredis` — the
`kntgraph-testing` §7.1 behaviour bar, not a mock.

Before/after on the same probe:

```
BEFORE:  3 distinct event_ids  -> EventLog stream length 3
AFTER:   1 event_id             -> EventLog stream length 1
```

---

## 6. Alternatives considered

| Alternative | Pros | Cons | Verdict |
| --- | --- | --- | --- |
| **A. Remove `swept_at` from the payload entirely** | Simplest; the identity becomes stable with no extra code | Loses the audit information ("when did the sweeper notice this?") | **Rejected** — the information is worth keeping |
| **B. Keep the payload; exclude `swept_at` from the identity** (this ADR) | Keeps the audit information; the identity is explicit rather than incidental | Two payload shapes to maintain (payload vs identity inputs) | **Recommended** |
| **C. Make the identity a function of `request_event_id` only** | Simplest possible identity | Two different failures for the same request (e.g. a different error message) would collide | **Rejected** — over-collapses |
| **D. Move the dedup to the EventLog's `causation_id`** | No change to the sweeper | The EventLog's dedup key is `event_id`; adding a second key changes the EventLog's contract for every event type | **Rejected** — the fix belongs where the defect is |
| **E. Persist the `_emitted_failures` set in Redis** | Cross-process dedup without touching the identity | Adds a Redis round-trip per stale request and a new key to maintain; does not fix the identity, which other consumers also key on | **Rejected** — treats the symptom |

---

## 7. Rollout

The change is backward compatible on the wire (the payload is
unchanged) and additive in behaviour (fewer duplicate events). It
can ship in the next minor release with no configuration.

**Deployment note for `soldi/backoffice`:** the existing duplicated
events stay in the agent's EventLog — this fix prevents new ones.
A one-off cleanup of the duplicated `ttl_expired` events is
optional; they are inert once the completion-driven eviction
(ADR-044) removes the request from the slot.

---

## 8. References

- **`runner/tool_call_ttl_sweeper.py`** — the sweeper; the fix is in
  `_build_failed_event`.
- **`core/event/id_helpers.py`** — `_legacy_event_id` (the hash that
  includes `data`) and `generate_deterministic_event_id`.
- **`infra/redis/_event_log/_idempotency.py`** — the EventLog's
  `event_id` dedup whose guarantee this ADR restores.
- **`core/world/projection_tool_calls.py:380-381`** — `expires_at =
  requested_at + ttl`, the reason `expired_at` is stable.
- **`concordos/saga/_timeout_system.py:107-112`** — the framework's
  stable-identity precedent.
- **`concordos/fsm/_system.py:155-157`** — the framework's
  cursor-gate precedent.
- **`soldi/backoffice/docs/features/012-diagnostico-duplicacao-execucao-tools.md`**
  — the vertical's diagnostic; §3.2 records the production
  measurement this ADR is based on.
