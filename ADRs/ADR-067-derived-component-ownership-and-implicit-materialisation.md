<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-067: Ownership Rule for Derived Components and Implicit Memory Materialisation

- **Status:** Implemented
- **Date:** 2026-08-31
- **Author:** kinetgraph architecture team
- **Related to:**
  - [ADR-042](./ADR-042-Agents-Memory-Model-usage.md) — Agents Memory Model (hydration)
  - [ADR-044](./ADR-044-Tool-call-Overlay-Accumulation.md) — Tool-call overlay accumulation
  - [ADR-059](./ADR-059-Domain-Memory-ECS-Components.md) — Domain Memory via ECS Components
  - [ADR-014](./ADR-014-Continuity-Tier.md) — Continuity Tier

## 1. Context and Problem

Two defects surfaced together in `kntgraph==0.14.1`, found while
investigating the soldi/backoffice issue
(`docs/issues/profile-preference-set-collision.md` downstream)
where readers of the string-keyed
`view.components["profile.preference_set"]` snapshot saw state
silently disappear.

### 1.1 The ownership hole in `_apply_event`

The default domain fold built the new components dict from the
event alone, then merged the previous view's derived components
with `dict.update(preserved)`:

```python
new_components.update(preserved)   # preserved OVERWRITES the new
```

Because **every** subclass of `DomainComponent` passes
`_is_derived_component_key`, a typed component registered via
`@domain_component` was "preserved" over the value the current
event had just produced. Two events of the same registered type
on the same agent left the **first** value on the view — a
First-Event-Wins freeze that contradicts the documented
last-event-wins contract (pinned for string keys by
`test_domain_replaces_components`). The auto-hydration of
ADR-059 never updated a component after its first event.

### 1.2 The `*.created` gate starved the memory hydration

`project_memory` (ADR-042) materialised `ProfileComponent` /
`ContinuityComponent` only when the agent had a
`profile.created` / `continuity.created` event. Verticals that
emit bare `profile.preference_set` events (the backoffice
`tenant_sync_system` convention) never saw a component at all,
even though the events carried real state. The Redis-tier folds
in `kntgraph.memory` had the same gate, so the two layers
disagreed with nothing in between to catch it.

## 2. Decision

### 2.1 Ownership rule: an event writes only what it produced

`_apply_event` now applies a single rule: **a domain event may
only write the component keys it produced itself** via
`_extract_components_from_event`. Every other derived key
survives untouched:

- **String keys** in `_DERIVED_COMPONENT_KEYS`
  (`tool_requests`, `tool_completions`) are **overlay-owned**. A
  domain event never overwrites them — even when its
  `event_type` is literally `"tool_requests"`. Such a collision
  is a caller mistake: the overlay re-derives the slot from the
  events after the fold, so the event's payload is ignored and
  the mistake is logged at WARNING (`projection.overlay_key_collision`)
  instead of failing the fold.
- **Class keys** (typed components) follow last-event-wins when
  the event re-derives the class — the class-key mirror of
  `test_domain_replaces_components`. When it does not, the
  previous component survives (memory components installed by
  `project_memory`, or a typed component a previous event
  installed).

Event `event_type`s are dotted namespaces (`tool.chat_llm.requested`);
a domain event colliding with an overlay slot name is implausible
in practice, and the WARNING makes it visible if it happens.

### 2.2 Implicit materialisation for profile and continuity

A memory component materialises when the batch (or the
EventLog stream) carries **any** event of its tier — no explicit
`*.created` required. A profile or continuity without `created`
carries `created_at == 0.0`: the honest value for "never
formally created", not a failure. The identity fields
(`tenant_id` / `user_id`) are recovered from the agent_id
convention (`profile:{tenant_id}:{user_id}`) when the events do
not carry them; a one-part id (`profile:{tenant_id}`) yields an
empty `user_id` — the vertical owns the convention.

`*.created` remains meaningful (it stamps the real creation time
and seeds preferences / tier) but is no longer a gate. The rule
applies uniformly to:

- `core.world.projection_memory` (`_fold_profile`, `_fold_continuity`),
- the Redis-tier folds (`memory.profile._fold_profile_events`,
  `memory.continuity.fold._fold_continuity_events`).

**Session is the deliberate exception.** A session's identity
(`user_id`, `tenant_id`, `session_id`) is payload-borne
(`session.started`) and not recoverable from the agent_id, and
`SessionManager.start` already guarantees the event exists. The
explicit gate stays for session.

### 2.3 Why not auto-emit `created` at spawn

Auto-emitting `*.created` when an agent first appears was
rejected: spawn is not an interceptable hook (agents materialise
from the first event that references them), a synthetic event
would pollute the EventLog audit trail (continuity is LGPD
scope), and the fold — not the emission — is the shared
chokepoint that reaches every call-site. Implicit
materialisation delivers the same ergonomics with zero new
events.

## 3. Consequences

### Positive impacts

- Registered `@domain_component` components now update on
  re-emission (last-event-wins), unblocking the typed-read
  migration path for downstream verticals (the backoffice
  `AccountingOpenedComponent` was frozen at its first value).
- Verticals that emit bare `profile.*` / `continuity.*` events
  get `ProfileComponent` / `ContinuityComponent` for free in
  the hydration path — no bootstrap event required.
- The hydration projection and the Redis-tier folds agree on
  the materialisation contract.
- Overlay slots are inviolable by construction: no domain
  event, however malformed, can clobber `tool_requests` /
  `tool_completions`.

### Trade-offs / watch-outs

- `read()` on `ProfileManager` / `ContinuityManager` can now
  return a state with `created_at == 0.0` where it previously
  returned `None`. Callers that treated `None` as "no profile"
  must check the state's fields instead. This is a contract
  change and the reason for the minor version bump.
- The WARNING on overlay-key collision is a signal, not a
  guard; a noisy deployment should treat the log line as a bug
  report against the emitting vertical.
- A `profile.created` that arrives **after** bare
  `profile.preference_set` events (reordering in replay)
  re-stamps `created_at` but preserves the accumulated
  preferences — the handlers are additive, so replay order
  between materialising events does not lose data.