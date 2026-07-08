<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Threat Model

This document is the formal threat model that motivates the
zero-trust levels. It is referenced by [README.md](./README.md)
and by the per-level documents ([signing.md](./signing.md),
[authorization.md](./authorization.md),
[anchor.md](./anchor.md)).

> **Audience**: security reviewers, compliance teams,
> architects evaluating the FMH for sensitive deployments.

---

## 1. Scope

### In scope

- Events at rest in Redis Streams (`fmh:agents:{id}:events`).
- Events in flight through `EventLog.append`, `EventLog.read`.
- Downstream projections (FalkorDB, knowledge graphs).
- The producer side: keys, signing, policies.
- The verifier side: any process that reads events and
  applies business logic.

### Out of scope (other layers handle these)

- Network MITM between producer and Redis. **Mitigation**:
  deploy Redis with TLS (`rediss://`).
- Confidentiality of event payloads. **Mitigation**:
  `SecureComponent` (ADR-014).
- Producer's host compromise. **Mitigation**: HSM/KMS at L4.
- Redis admin access. **Mitigation**: ACLs, audit log,
  separation of duties.

### Trust assumptions

The FMH assumes:

- The host running the framework is **not** root-compromised
  (otherwise no defence works).
- The Redis instance is **not** arbitrary-write accessible
  (ACLs are configured; v2 adds signature-aware ACLs).
- The Python interpreter is the canonical `cpython` (no
  malicious packages).

These are the **trusted computing base** (TCB). The zero-trust
levels shrink what an attacker can do **inside** these
assumptions; they do not replace them.

---

## 2. Adversary model

| Adversary | Capability | Goal | Defended by |
|---|---|---|---|
| **External attacker** | Network access to Redis, no credentials. | Read events; probe for misconfig. | TLS, ACLs (out of scope). |
| **Malicious operator** | Redis read+write access (e.g. ops on shared host). | Forge events; mutate history. | L1 (forgery), L3 (retro-editing). |
| **Compromised agent** | Valid signing key + Redis access for one `agent_id`. | Emit events outside its scope; replay; flood. | L2 (authz, rate limit, revoke). |
| **Compromised consumer** | Valid read access to Redis + knowledge writer. | Project forged knowledge; corrupt downstream. | L3 (anchor + re-verify before projection). |
| **Insider with long-term key access** | Access to `long_term_key` (L3) or `KeyRegistry`. | Forge anchors; hide retro-editing. | L4 (HSM), operational rotation discipline. |

The **escalation path** is left-to-right: as an attacker
gains more capability, the FMH raises the bar via L1 → L2 →
L3 → L4. **No level fully eliminates the insider**; the goal
is to **make the attack detectable and containable**.

---

## 3. Threat catalogue

Each threat is enumerated with:

- **T-ID**: stable identifier.
- **Asset**: what is at risk.
- **STRIDE**: Spoofing / Tampering / Repudiation / Information
  disclosure / Denial of service / Elevation of privilege.
- **Mitigation**: which level(s) address it.
- **Residual risk**: what remains after mitigation.

### T-01: Event forgery (no signature)
- **Asset**: audit log; downstream projections.
- **STRIDE**: S, T, R.
- **Description**: attacker with Redis write emits
  `process.completed` for an unfinished pedido.
- **Mitigation**: L1 (signature verify rejects unsigned or
  wrongly-signed events when `require_signatures=True`).
- **Residual**: if attacker compromises signing key, T-02.

### T-02: Compromised signing key
- **Asset**: authentication of producer.
- **STRIDE**: S.
- **Description**: attacker steals private key of
  `session-42`; emits validly-signed events.
- **Mitigation**: L2 (`revoke(agent_id, key_epoch)` rejects
  future events under the compromised key); L4 (HSM-backed
  keys never leave hardware).
- **Residual**: events signed before revocation still verify
  (by design; auditor must investigate).

### T-03: Over-privileged agent
- **Asset**: integrity of business process.
- **STRIDE**: E.
- **Description**: an agent that should only emit
  `pedido.received` emits `process.cancelled`.
- **Mitigation**: L2 (`CapabilityPolicy.allowed_event_types`).
- **Residual**: confused-deputy within the allowed event types
  (e.g. `pedido.received` with malicious payload).

### T-04: Replay
- **Asset**: idempotency, freshness.
- **STRIDE**: T, R.
- **Description**: attacker reads an event from the Stream
  and re-emits it on a different consumer.
- **Mitigation**: `event_id` UUID5 dedup; L2 rate limit.
- **Residual**: cross-window replay if `event_id` is
  reproducible (UUID5 is deterministic but includes
  `causation_id`; replay with same causation is detectable
  but not blocked by L1 alone).

### T-05: Retro-editing
- **Asset**: audit trail integrity.
- **STRIDE**: T, R.
- **Description**: attacker mutates a historical event's
  payload in Redis.
- **Mitigation**: L3 (anchor chain + detector).
- **Residual**: if the anchor `long_term_key` is compromised,
  attacker rewrites anchors too. L4 mitigates.

### T-06: DoS via event flooding
- **Asset**: availability.
- **STRIDE**: D.
- **Description**: attacker emits millions of events to fill
  Redis or starve verifiers.
- **Mitigation**: L2 (`max_event_rate_per_sec`); Redis
  Stream auto-trim.
- **Residual**: rate limit Redis is a single point; deploy
  with `rate_limit_fail_open=False`.

### T-07: Compromise of consumer (downstream projection)
- **Asset**: knowledge graph integrity.
- **STRIDE**: T.
- **Description**: FalkorDB projector with Redis read access
  writes forged nodes/edges.
- **Mitigation**: L3 (`ProcessLearnerSystem` re-verifies
  anchor before projection).
- **Residual**: if projector is fully compromised, it can
  bypass verify; L4 includes attestation.

### T-08: Compromised long-term key
- **Asset**: tamper-evidence.
- **STRIDE**: T, R.
- **Description**: attacker with `long_term_key` forges
  anchors; can rewrite history without detection.
- **Mitigation**: L4 (HSM); operational rotation.
- **Residual**: insider with key access; L4 reduces the
  window but does not eliminate.

### T-09: Algorithm compromise
- **Asset**: signature security.
- **STRIDE**: T.
- **Description**: a future attack (e.g. quantum) breaks
  Ed25519.
- **Mitigation**: algorithm agility (`alg: "<name>-v<n>"`).
  L4 ships ML-DSA-65.
- **Residual**: migration window where old signatures are
  untrusted but historical anchors cannot be re-signed.

### T-10: Policy confusion
- **Asset**: authz integrity.
- **STRIDE**: E.
- **Description**: policy declaration has a typo or stale
  entry; legitimate agent is over- or under-privileged.
- **Mitigation**: YAML schema validation; `policy_dry_run`
  for safe rollout; audit log of policy changes.
- **Residual**: human error.

---

## 4. STRIDE coverage matrix

| Level | S | T | R | I | D | E |
|---|---|---|---|---|---|---|
| L0 | – | partial¹ | partial² | – | – | – |
| L1 | ✅ | – | – | – | – | – |
| L2 | ✅ | – | partial³ | – | ✅ | ✅ |
| L3 | ✅ | ✅ | ✅ | – | ✅ | ✅ |
| L4 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

¹ dedup by `event_id` catches content-level tampering.
² correlation_id provides audit chain.
³ revocation provides partial non-repudiation (rejected
  events are auditable, accepted events are not).

---

## 5. Security objectives

For a **production L3 deployment**, the FMH claims:

| Objective | Statement |
|---|---|
| **O-1: Authenticity** | Every event with `signature != None` was emitted by the holder of the matching private key at the signing time. |
| **O-2: Authorisation** | Every event accepted by `EventLog.append` was within the emitter's `CapabilityPolicy`. |
| **O-3: Tamper-evidence** | Any retroactive edit to events between two anchors is detectable by the offline detector. |
| **O-4: Containment** | Revocation of a compromised key blocks future events within `cache_ttl + 1 anchor window` (default ~6 minutes). |
| **O-5: Auditability** | An independent auditor with public keys + Redis access can verify the chain offline. |
| **O-6: Algorithm agility** | Migration to a new algorithm is a versioned field; no breaking change to existing verifiers. |

### Residual risks (L3)

- Insider with `long_term_key` access (T-08) — L4 mitigates.
- Confused-deputy within allowed event types (T-03) —
  addressed by application-level validation, not FMH.
- Compromised host (TCB assumption violated) — out of scope.

---

## 6. Mapping to compliance frameworks

| Framework | Control | FMH level |
|---|---|---|
| **LGPD Art. 46** | Security measures for personal data | L1 (signed audit trail) |
| **LGPD Art. 48** | Notification of security incidents | L2 (rate limit + audit log) |
| **LGPD Art. 50** | Good practices and governance | L3 (tamper-evidence + audit API) |
| **SOC 2 CC6.1** | Logical access controls | L1+L2 |
| **SOC 2 CC7.2** | System monitoring | L2 (rate limit alerts) + L3 (anchor alerts) |
| **SOC 2 CC7.3** | Incident detection | L3 (retro-editing detector) |
| **HIPAA §164.312(b)** | Audit controls | L1+L3 |
| **HIPAA §164.312(c)** | Integrity controls | L1+L3 |
| **HIPAA §164.312(d)** | Person authentication | L2 |
| **PCI DSS 4.0 §10.2** | Audit log integrity | L1+L3 |
| **PCI DSS 4.0 §10.5** | Log integrity monitoring | L3 |
| **NIST SP 800-207** §3.1 | Zero Trust Architecture | L1+L2+L3 (partial; see below) |

### NIST SP 800-207 mapping

NIST 800-207 §3.1 enumerates zero-trust principles. The FMH
maps as follows:

| Principle | FMH level |
|---|---|
| All data sources are considered security objects | L3 (everything is signed) |
| All communication is secured regardless of location | out of scope (TLS) |
| Access to individual enterprise resources is on a per-session basis | L2 (per-event policy) |
| Access is governed by dynamic policy | L2 (`policy_dry_run`, hot-reload via RedisPolicyRegistry in v2) |
| The enterprise monitors and measures the integrity and security posture of all assets | L3 (anchor + detector) |
| All resource authentication and authorization are dynamic and strictly enforced | L2+L3 |
| The enterprise collects as much information as possible about the current state of assets and uses it to improve security posture | L2 audit log + L3 anchor API |

The FMH is **not** a complete zero-trust architecture in the
NIST sense (network, identity provider, runtime attestation
are out of scope). It is the **event-substrate layer** of one.

---

## 7. Threat modelling new deployments

For each new deployment:

1. **Identify the assets**: what data, what decisions, what
   compliance.
2. **Identify the adversaries**: who would attack; what
   capability.
3. **Map to threats**: T-01 through T-10 (or add new).
4. **Choose the target level** per asset:
   - Public, low-value data → L0.
   - Internal business data → L1.
   - Multi-tenant data → L1+L2.
   - Regulated data → L1+L2+L3.
5. **Document residual risks** and the operational runbook
   (key rotation, revocation, anchor monitoring).
6. **Plan the upgrade path**: each level is opt-in; no need
   to start at L3 if L1 is sufficient.

---

## 8. See also

- [README.md](./README.md) — overview
- [signing.md](./signing.md) — L1 detail
- [authorization.md](./authorization.md) — L2 detail
- [anchor.md](./anchor.md) — L3 detail
- [ADR-016](../../ADRs/ADR-016-Event-Signing.md) — L1 design
  record
