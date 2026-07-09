<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Zero-Trust Security in the Kinetgraph

The Kinetgraph is a content-addressed, event-sourced framework. By
default, events are **integrity-protected** (UUID5 over payload)
but **not authenticated** — there is no proof that an event with
`agent_id = "session-42"` was actually emitted by the agent that
owns `session-42`. This document is the public-facing companion to
the four ADRs that close this gap.

> **Audience**: framework integrators, security reviewers,
> operators planning multi-tenant or regulated deployments.
>
> **Scope**: events at rest in Redis Streams, in flight via
> `EventLog`, and downstream (projections, knowledge, audit).
> Out of scope: in-memory state, transient Redis keys, network
> MITM (covered by TLS).

---

## 1. Threat model

The Kinetgraph considers three threat classes:

| Class | Example | Default defence |
|---|---|---|
| **Forgery** | Attacker with Redis write access emits `process.completed` for a pedido that did not finish. | None today; signed events (L1). |
| **Over-privilege** | Authenticated agent emits `process.cancelled` even though it should only emit `pedido.received`. | None today; capability policies (L2). |
| **Retro-editing** | Attacker mutates historical events in Redis; auditor cannot tell. | None today; Merkle anchor (L3). |

Out of scope (handled by other layers):

- **Confidentiality** of payloads — covered by `SecureComponent`
  (ADR-014).
- **Network MITM** — covered by `rediss://` (deployment concern).
- **Producer's private key compromise** — covered by revocation
  (L2) and HSM (L4).

---

## 2. The four levels

The Kinetgraph organises security into four progressive levels. Each
level preserves compatibility with the previous one (load of
events written under lower levels continues to work). Levels
build on each other; you cannot skip a level without explicit
acknowledgement.

| Level | Theme | What it adds | ADR |
|---|---|---|---|
| **0** | Baseline (v0.7.0) | Content addressing, dedup by `event_id`. | — |
| **1** | **Authenticated Producers** | Ed25519 signature per event, JCS canonical bytes, in-process keys. | [ADR-016](../ADRs/ADR-016-Event-Signing.md) |
| **2** | **Authorised Producers** | Per-agent `CapabilityPolicy` (RBAC over `event_type`), key revocation, rate limit. | ADR-017a (proposed) |
| **3** | **Continuous Verification** | Verification cache (LRU+TTL), Merkle anchor chain, retro-editing detector. | ADR-017 (proposed) |
| **4** | **Hardware-Backed** | Vault/KMS, ECDSA P-256, ML-DSA-65 (PQC), BLS aggregate, third-party transparency (sigstore Rekor). | ADR-018 (planned) |

### Visual progression

```
L0: Event ─────────── Redis (signed: no)
L1: Event + Signature ─── Redis (signed: yes, opt-in)
L2: Event + Signature + Policy ─── Redis (authz enforced)
L3: + Anchor chain ─── Redis (tamper-evident)
L4: + HSM-backed keys + external transparency log
```

---

## 3. Quick reference: what to read

| You are... | Read |
|---|---|
| Evaluating Kinetgraph for a single-tenant deployment | This document (overview) + [signing.md](./signing.md) |
| Deploying multi-tenant, audit-required | This + [signing.md](./signing.md) + [authorization.md](./authorization.md) |
| Operating in a regulated vertical (LGPD/SOC 2/HIPAA) | All four documents, in order |
| Implementing or reviewing L1 code | [signing.md](./signing.md) §4 (API) + [ADR-016](../ADRs/ADR-016-Event-Signing.md) |
| Implementing L2 policies | [authorization.md](./authorization.md) + ADR-017a (proposed) |
| Implementing L3 anchors | [anchor.md](./anchor.md) + ADR-017 (proposed) |
| Threat-modelling a specific deployment | [threat_model.md](./threat_model.md) §3 (checklist) |

---

## 4. How to choose a target level

### Single-tenant, internal prototype
**L0 is acceptable.** You trust the operator; the framework's
content-addressed dedup is enough. Document the assumption.

### Single-tenant, production
**L1 is the minimum.** Signing events costs ~30µs/event; the
operational cost is negligible. Enable `require_signatures=True`
in `EventLog` constructor.

### Multi-tenant, shared Redis
**L1 + L2.** Without L2, any signed agent can emit any event
type. The `CapabilityPolicy` per `agent_id` is the cheapest
control. kinetgraph ships with L1 + L2 enabled by default.

### Regulated (health, finance, public sector)
**L1 + L2 + L3.** The Merkle anchor gives auditors a
cryptographic chain they can replay independently. Cost:
~500 LoC + an `AnchorScheduler` background task.

### Multi-region, HSM-mandated, post-quantum
**L1 + L2 + L3 + L4.** Triggers: regulator mandate, NSA CNSA 2.0
timeline, contractual requirement.

---

## 5. What each level does NOT give you

Honesty about the model:

| Threat | Defended by | NOT defended by |
|---|---|---|
| Forgery (L1+) | Ed25519 signature + JCS canonical bytes | Compromised producer's private key (L2 revoke / L4 HSM) |
| Over-privilege (L2+) | `CapabilityPolicy` deny-list | Confused-deputy inside a permitted event_type (audit only) |
| Retro-editing (L3+) | Hash-chain anchor + audit API | Compromise of the long-term anchor key (L4 HSM) |
| Replay (L1+) | Dedup by `event_id` (UUID5) | Cross-key replay within dedup window (rate limit L2) |
| Confidentiality | `SecureComponent` (ADR-014), `rediss://` | — |

The Kinetgraph is not a single-vendor security stack; it is a
**content-addressed event substrate**. Other layers (network,
storage, runtime) carry their part of the zero-trust burden.

---

## 6. Migration paths

### From L0 to L1
- **Backwards-compat**: events written under L0 (`signature=None`)
  load and replay normally. Verification is **opt-in**.
- **Action**: deploy L1 producer (signing enabled). Optionally
  enable `require_signatures=True` on `EventLog`.
- **No retroactive signing** in v1. Use `scripts/resign_old_events.py`
  (v1.1) if you need to bring old events into the signature
  chain.

### From L1 to L2
- **Backwards-compat**: signing continues to work; policy is
  additive.
- **Action**: declare `CapabilityPolicy` per `agent_id` in your
  configuration. kinetgraph accepts a YAML block `agents:` with
  `allowed_event_types`, `denied_event_types`, and
  `max_event_rate_per_sec`.

### From L2 to L3
- **Backwards-compat**: anchor is opt-in per agent.
- **Action**: enable `AnchorScheduler` for the agents you want to
  anchor. Provide a `long_term_key` (separate from the per-event
  signing key). Verify anchors are written via the audit API
  (`GET /agents/{id}/anchors`).

### Across levels
The framework guarantees that events written under a lower level
are **loadable** under any higher level. Verification may return
`False` (unverified) but never `Exception`.

---

## 7. Reference architecture (L3 deployment)

```
┌──────────────────────────────────────────────────────────┐
│ Producer (kinetgraph.PedidoRunner or your code)          │
│                                                          │
│  ┌─────────────┐     ┌─────────────────────────────┐    │
│  │ Event.build │ ──► │ canonical_event_bytes(...)  │    │
│  └─────────────┘     │   (RFC 8785 JCS)            │    │
│                      └──────────────┬──────────────┘    │
│                                     │                    │
│                          ┌──────────▼──────────┐         │
│                          │  sign_event(...)    │         │
│                          │   (ed25519, key_ep) │         │
│                          └──────────┬──────────┘         │
│                                     │                    │
│                          ┌──────────▼──────────┐         │
│                          │ Event(signature=..) │         │
│                          └──────────┬──────────┘         │
└─────────────────────────────────────┼────────────────────┘
                                      │
                                      ▼
                              ┌──────────────┐
                              │   EventLog   │
                              │  (Redis)     │
                              │  + verify()  │
                              │  + policy()  │
                              │  + anchor()  │
                              └──────┬───────┘
                                     │
            ┌────────────────────────┼────────────────────────┐
            ▼                        ▼                        ▼
     ┌─────────────┐          ┌─────────────┐          ┌─────────────┐
     │ FalkorDB    │          │ Auditor     │          │ Downstream  │
     │ (knowledge) │          │ (anchors)   │          │ consumers   │
     │             │          │             │          │             │
     │ re-verify   │          │ GET /anchor │          │ cache TTL   │
     │ anchor      │          │             │          │             │
     └─────────────┘          └─────────────┘          └─────────────┘
```

---

## 8. Operational checklist (L3)

For production L3 deployment:

- [ ] All Redis connections use `rediss://` (TLS).
- [ ] `KeyRegistry` is hydrated from a secret store (Vault KV or
  equivalent) at boot.
- [ ] `EventLog.require_signatures=True` for all agents that
  emit business events.
- [ ] `CapabilityPolicy` declared for every `agent_id` that can
  emit to the `EventLog`.
- [ ] `AnchorScheduler` enabled for agents covered by retention
  / audit requirements.
- [ ] Audit API (`GET /agents/{id}/anchors`) is exposed to the
  security / compliance team (read-only credentials).
- [ ] Revocation list (`knt:revocations:{agent_id}`) is
  replicated to all verifiers within the revocation window
  (default: cache TTL + 1 anchor window).
- [ ] Anchor `long_term_key` is rotated quarterly; rotation
  script runbook documented.
- [ ] Monitoring: alert on `signature_verify_failures > 0`,
  `policy_rejections > 0.1%`, `anchor_mismatch > 0`.

---

## 9. Frequently asked questions

### Q: Does signing break replay?
No. The signature is over the JCS bytes of the event; replay
re-creates the same bytes; the signature still verifies. Dedup
by `event_id` continues to short-circuit replays.

### Q: Can I sign events written under L0?
Not in v1. Use `scripts/resign_old_events.py` (v1.1) to walk
the EventLog and re-sign under the current key.

### Q: Why Ed25519 and not ECDSA P-256?
Smaller (32B pubkey vs 65B), faster, deterministic, and RFC 8785
makes cross-implementation verification trivial. ECDSA P-256 is
available in v2 as `alg: "ecdsa-p256-sha256-v1"` for FIPS-only
deployments.

### Q: Why JCS and not my current `json.dumps`?
`Event.to_json` (current) is not RFC 8785-compliant; signatures
computed over it would not verify across Python versions or
implementations. JCS is the canonicalisation contract.

### Q: Do I lose performance?
Sign + verify is ~30µs per event on a modern CPU. The kinetgraph
MVP (5 events/pedido, ~1.5s end-to-end) loses ~200µs to signing.
Negligible.

### Q: What about HSM / KMS today?
`KeyRegistry` is a Protocol; the v1 impl is `InMemoryKeyRegistry`
(dict). A v2 implementation against HashiCorp Vault or AWS KMS
plugs in without touching call sites.

### Q: Is the Kinetgraph "zero-trust" out of the box?
**No.** L0 is content-addressed but not authenticated. Calling
the Kinetgraph "zero-trust" requires at least L1 + L2, ideally L3. The
levels exist to make the upgrade path explicit.

---

## 10. See also

- [signing.md](./signing.md) — Level 1 detail
- [authorization.md](./authorization.md) — Level 2 detail
- [anchor.md](./anchor.md) — Level 3 detail
- [threat_model.md](./threat_model.md) — formal threat model
- [ADR-016](../ADRs/ADR-016-Event-Signing.md) — Level 1 design
  record
- [ADR-001](../ADRs/ADR-001-Arquitetura.md) — base architecture
- [NEXT_STEPS.md](../../../NEXT_STEPS.md) — Path H (Zero-Trust
  Hardening)
