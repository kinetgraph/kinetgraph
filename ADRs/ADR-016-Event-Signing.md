<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-016: Event Signing (Cryptographic Authentication of Event Producers)

| | |
|---|---|
| **Status** | Proposed |
| **Date** | 2026-06-22 |
| **Deciders** | @adriano |
| **Supersedes** | — |
| **Related** | ADR-001 (Architecture), ADR-002 (Replay), ADR-005 (Idempotency), ADR-012 (IntentRouter), ADR-015 (fmh_office) |

---

## 1. Context

The FMH event-sourcing substrate is **content-addressed
but not authenticated**. Every `Event` carries a
deterministic `event_id` (UUID5 over
`agent_id | causation_id | event_type | payload`),
which gives strong *integrity* — the framework can
detect any mutation of an event already written to the
`EventLog` because the recomputed `event_id` would not
match the one stored in `knt:eventids:{event_id}`.

What the framework does **not** give us is
*authentication*: there is no way to verify that an
event with `agent_id = "session-42"` was actually
emitted by the agent that owns `session-42`, nor that
the binding `agent_id → producer` was set by an
authority the system recognises. Anyone with write
access to Redis (operator on the same host, a
misconfigured exporter, a compromised dependency) can
emit events with arbitrary `agent_id` values and the
`EventLog.append` will accept them. The dedup
mechanism keys on `event_id`, not on signer identity.

This is a gap in the threat model that the framework
documents (ADR-001 §6: "operational and audit
concerns") but does not currently solve. Three
concrete scenarios motivate this ADR:

1. **Multi-tenant operators** (proposed in
   `NEXT_STEPS.md` Path D). Tenant A must not be able
   to emit events for tenant B by guessing
   `agent_id`. The framework's `RedisAPIKeyVerifier`
   (ADR-012) only protects the HTTP gateway; once
   the request is past auth, the resulting
   `EventLog.append` does not verify that the
   in-process producer is the one the API key
   authorises.

2. **Cross-agent process hand-offs** (e.g. the
   `fmh_office` engine emitting a `process.completed`
   event on behalf of an executor; the
   `ProcessLearnerSystem` reading it and projecting
   to FalkorDB). Today there is no cryptographic
   proof that the terminal event was emitted by the
   engine, not by a malicious insider with Redis
   access.

3. **Audit and tamper-evidence.** Regulated
   verticals (`fmh_clinic`, financial products)
   require that the operator can demonstrate that
   the audit log was not retroactively edited.
   Today the only mechanism is "trust the Redis
   host". Forward-only signing (this ADR) gives
   the auditor a per-event signature they can
   verify against the producer's public key; the
   `KEYSTORE` provides the public-key registry.

The framework's `Event` (ADR-002) is a frozen
dataclass, but its constructor accepts a
`signature: Optional["Signature"]` keyword in this
ADR. This is the first ADR to add a field that the
`to_dict` roundtrip carries across the wire; the
proposal is **non-breaking** (existing callers that
never set `signature` keep working; readers that
find `signature=None` simply skip verification).

---

## 2. Problem

Today, the framework offers:

- **Content addressing** (`event_id` UUID5).
  ✅ Integrity. ❌ Authentication.
- **Causal chain** (`causation_id`,
  `correlation_id`). ✅ Auditability of *flow*. ❌
  Authentication of *who emitted*.
- **Idempotency** (`knt:eventids:{event_id}`
  SETNX in `EventLog.append`). ✅ Replay-safety.
  ❌ Source authentication.
- **DLQ** (`DeadLetterQueue` in ADR-009). ✅
  Failure capture. ❌ Forgery detection.

None of these primitives is a substitute for a
*signature*: a value bound to (a) the canonical bytes
of an event, (b) a private key held only by the
producer. The producer publishes the public key; any
verifier can confirm that **only the producer of
the matching private key could have produced this
event**, given the bytes.

We want to add signing without breaking:

- The frozen-dataclass identity of `Event`.
- The wire format of `EventLog` (Redis Streams;
  backward-compat for already-written events).
- The deterministic `event_id` (still keyed on
  payload; signature is *additional* metadata).
- The HTTP gateway's request/response shape
  (ADR-012; signature is transparent to the client).
- The performance budget (the `fmh_office` MVP
  pumps ~5 events per pedido; we cannot add > 50µs
  per event without breaking the 1.5s smoke test
  budget).

---

## 3. Requirements

### 3.1 Functional

1. The producer of an `Event` can sign it with a
   private key bound to `agent_id`.
2. A verifier holding the matching public key (and
   the `Event` bytes) can confirm the signature.
3. The framework's `EventLog` can store and roundtrip
   the signature without changing the `event_id` or
   the `payload` semantics.
4. The framework supports **aggregation** of
   multiple signatures on a related set of events
   (e.g. a "session" that emits many events) — but
   v1 may ship concatenation only; the API must
   reserve room for true aggregation in v2.
5. The framework supports **per-`agent_id` key
   registration**: a process can ask the `KeyRegistry`
   for the public key of any `agent_id` it knows
   about, and for its own private key (subject to
   authorisation).

### 3.2 Non-functional

- **Canonical form**: the bytes that are signed
  must be the same across Python versions,
  platforms, and (eventually) other language
  clients. RFC 8785 (JCS) is the agreed-on
  canonicalisation.
- **Performance**: signing + verification must add
  < 50µs per event on a typical `fmh_office`
  pipeline (PyCA `cryptography` is ~10k sigs/s on a
  modern CPU, well under budget).
- **Backward compat**: events written *before* this
  ADR (signature absent) must continue to load and
  read; verification is **opt-in** at the consumer.
- **Algorithm agility**: the signature carries an
  explicit `alg` field. Migration to a new algorithm
  (e.g. post-quantum) is a versioned field, not a
  code change.
- **Forward-only**: events written *after* this
  ADR's release are signed; events written *before*
  are not retroactively signed. A future utility
  (v1.1) may scan and re-sign; it is out of scope
  here.

### 3.3 Threat model (in scope)

- An attacker with **write access to the
  `EventLog`** (Redis) can mutate or insert events.
- The attacker can also rotate the `agent_id`,
  `event_type`, and `payload` to bypass dedup.
- The attacker's goal: forge a `process.completed`
  event for a pedido that did not actually complete.

**Out of scope (this ADR)**:

- An attacker with **read access** to the `EventLog`
  is not stopped by signatures (they are public).
  For confidential events, the framework already
  provides `SecureComponent` (per ADR-014, "memory
  tier continuity" §6) — a different problem.
- An attacker with the **producer's private key**
  (e.g. an insider with `/proc/{pid}/mem`) is not
  stopped. The HSM/KMS migration in v2 is the right
  mitigation; this ADR is compatible with it (the
  `KeyRegistry` is a Protocol).
- A **network attacker** that can MITM the Redis
  connection is not stopped (use TLS on Redis; out of
  scope for this ADR).

---

## 4. Decision

We adopt **per-event Ed25519 signatures over a JCS
canonical form of the event**, with the following
sub-decisions.

### 4.1 Algorithm: Ed25519 (RFC 8032, PureEdDSA)

**Chosen**: Ed25519 (PureEdDSA, RFC 8032 §5.1.6).

- 32-byte public key, 64-byte signature.
- ~10k sign/s and ~30k verify/s in Python with
  `cryptography` (PyCA, native OpenSSL).
- Deterministic (no RNG needed for nonces; sig is a
  pure function of message + key).
- Not in FIPS 186-5 (NIST only lists ECDSA, RSA, but
  EdDSA is on the FIPS 186-5 *draft* and accepted in
  many federal contexts; FIPS is not in scope for the
  current verticals).

**Why not ECDSA P-256**: smaller key (32B vs 65B
uncompressed) and faster signing. ECDSA P-256
remains a viable v2 (`alg: "ecdsa-p256-sha256-v1"`)
if a deployment requires FIPS. Ed25519 is the v1
default; the v2 codepath is in the algorithm-agility
shape (§4.5).

**Why not RSA**: 256-byte signatures for RSA-2048;
slow sign (1k/s); no advantage.

**Why not BLS12-381**: ~35× slower verify (per
IETF draft-irtf-cfrg-bls-signature §1.1);
`blspy` (Chia Network) is **archived** as of
Jul 2025; `chia_rs` is a 5 MB Rust binding. We do
not need N-of-M aggregation in v1. We reserve the
shape for BLS in v2 (the `AggregateSignature.alg`
field can carry `"bls12-381-v1"`).

### 4.2 Canonical form: JCS (RFC 8785)

**Chosen**: canonicaljson (cyberphone, RFC 8785
reference implementation in Python).

The current `Event.to_json` uses
`json.dumps(..., sort_keys=True, default=str)`. This
is **not** RFC 8785-compliant:

- Does not normalise Unicode
  (RFC 8785 §3.2.2.2 — e.g. U+0008 vs `\b`).
- Does not normalise floats
  (RFC 8785 §3.2.2.3 — IEEE 754 round-to-even).
- `default=str` may produce different representations
  across Python versions or between Python and other
  languages.

A signature computed over `to_json` will not verify
between Python 3.10 and 3.12 (let alone across
implementations). JCS fixes this at the cost of a
~50 KB pure-Python dependency.

**Why not a minimal in-tree JCS implementation**:
the spec is small but the corner cases (number
serialisation, surrogate pairs) are subtle; we
prefer the reference impl maintained by the RFC
author. We add a fallback path (PR 1 will
benchmark): if `canonicaljson` is too slow on
payloads > 1 KB, we ship a hand-rolled subset for the
`Event.to_dict` shape (whose data is bounded by the
user).

### 4.3 Shape: `signature` field on `Event`

```python
@dataclass(frozen=True, slots=True)
class Signature:
    """Cryptographic signature on a single event.

    Covers the JCS-canonical bytes of the event's
    ``to_dict()`` with ``signature`` field absent.
    """
    alg: str   # "ed25519-v1"
    pk: str    # base64 (32 bytes)
    sig: str   # base64 (64 bytes)
```

The `Signature` is added to `Event` as an
**optional** field:

```python
@dataclass(frozen=True, slots=True)
class Event:
    event_id: UUID
    agent_id: str
    event_type: str
    event_class: EventClass
    timestamp: datetime
    data: Mapping[str, Any]
    correlation: CorrelationContext
    causation_id: Optional[UUID] = None
    version: int = 1
    signature: Optional["Signature"] = None  # NEW
```

`to_dict()` includes `signature` when set;
`from_dict()` decodes it when present and passes
`None` otherwise. The `__post_init__` validates the
`Signature` shape (alg whitelist, base64 decodable,
pubkey/sig length match alg).

The `event_id` determinístico **does not change**:
it is still the commitment. The signature is the
authentication. This separation is intentional: the
`EventLog` dedup by `event_id` is independent of the
verification step.

### 4.4 Aggregation: concat-v1 (linear)

```python
@dataclass(frozen=True, slots=True)
class AggregateSignature:
    """v1: concatenation of N signatures.
    v2: BLS12-381 (single 96-byte sig)."""
    alg: str                      # "concat-v1" | "bls12-381-v1"
    signatures: tuple[Signature, ...]
```

Verification of `concat-v1`: for each `(sig, pk)`,
verify `sig` against the canonical event bytes under
`pk`. Cost: O(N · verify). This is acceptable when
N ≤ 50 (typical for "session-level" aggregation).

BLS aggregate (v2) is reserved: when an `AggregateSignature`
with `alg="bls12-381-v1"` is encountered, the
verifier dispatches to `chia_rs.AugSchemeMPL.aggregate_verify`
(1 pairing check for N signers, ~2.7 ms). This is
**not** in v1.

### 4.5 Algorithm agility

Every signature carries `alg: "<algorithm>-v<version>"`.
The verifier dispatches on `alg` against a whitelist:

- `"ed25519-v1"` (v1 default)
- `"ecdsa-p256-sha256-v1"` (v2 FIPS-friendly)
- `"bls12-381-v1"` (v2 aggregate)
- `"ml-dsa-65-v1"` (v3 quantum-safe; planned)

Unknown `alg` → verification returns `False` (NOT
`Exception`). The producer of an unknown algorithm
is rejected at signature creation time
(`__post_init__` whitelist). This is the same
pattern as `event_class`'s Literal.

### 4.6 Key storage: in-process v1, Protocol for v2

```python
class KeyRegistry(Protocol):
    """Resolves agent_id -> (public key, private key).

    v1: InMemoryKeyRegistry (dict). Keys lost on
    process restart; documented limitation.

    v2: VaultKeyRegistry / KmsKeyRegistry (HSM-backed).
    The Protocol is the integration point.
    """
    def public_key(self, agent_id: str) -> PublicKey: ...
    def private_key(self, agent_id: str) -> PrivateKey: ...
    def register(self, agent_id: str, priv: PrivateKey) -> None: ...
```

The MVP ships `InMemoryKeyRegistry`. Applications
that need durability can persist the keys (PEM
encoded) and re-hydrate at boot; this is a 10-line
utility, not part of this ADR.

### 4.7 Wire format: additive, non-breaking

**Redis Stream encoding** (`_event_to_redis`):
add a `signature` key (JSON-serialised
`Signature` dict, or `""` when absent).

**JSON wire format** (`to_json`): include
`signature` key when present; older consumers ignore
it (Pydantic v2 `model_config = ConfigDict(extra="ignore")`).

**`from_dict` / `_parse_event`**: decode `signature`
when present; pass `signature=None` to the `Event`
constructor when absent.

### 4.8 Enforcement is opt-in

`EventLog.append` accepts events with or without
signature. A new constructor parameter
`require_signatures: bool = False` controls whether
`append` rejects events with `signature=None`. v1
defaults to `False` (backward compat). Applications
that want strict authentication pass
`require_signatures=True`.

### 4.9 What is NOT in scope (this ADR)

- **Merkle Signed Tree Head (STH)**: per-agent
  Merkle trees with periodic STH signed by a
  separate long-term key. Detects retro-log
  rewriting. **v2**.
- **BLS aggregate**: see §4.4. **v2**.
- **Hash-then-sign for large payloads**: ed25519
  over JCS is fine for the FMH's payload sizes
  (< 1 KB typical). `Ed25519ph` (RFC 8032 §5.1) is
  the v1.2 path if payloads grow.
- **Quantum-safe (ML-DSA)**: see §4.5. **v3**.
- **Vault / KMS / HSM integration**: see §4.6.
  **v2**.
- **Cross-implementation**: we ship Python only in
  v1. The shape (§4.3) is intentionally
  implementation-agnostic; a Go or Rust client can
  reproduce the JCS bytes and verify against the
  public key.
- **Re-signing old events**: a read-only walker
  utility. **v1.1**.

---

## 5. Alternatives considered

### 5.1 ECDSA P-256 over the `to_json` current

- *Pro*: FIPS-approved; smaller surface area
  (no JCS dependency).
- *Con*: `to_json` is not canonical across Python
  versions; signatures will not verify between
  Python 3.10 and 3.12. **Rejected**.

### 5.2 Ed25519 over `to_json` current (no JCS)

- *Pro*: no new dependency.
- *Con*: same canonicalisation bug as §5.1.
  **Rejected** — would create a subtle
  forward-compat hazard.

### 5.3 BLS12-381 with `blspy`

- *Pro*: native aggregation, threshold sigs.
- *Con*: `blspy` is archived (Jul 2025); `chia_rs`
  is a 5 MB Rust binding; ~30× slower verify than
  ed25519. We do not need aggregation in v1.
  **Rejected for v1**; reserved for v2 (`alg:
  "bls12-381-v1"`).

### 5.4 HMAC for "signing" events

- *Pro*: stdlib-only; small.
- *Con*: HMAC is symmetric — any verifier can also
  sign. Defeats non-repudiation. **Rejected** —
  security model is broken.

### 5.5 Per-process signing (only the engine signs)

- *Pro*: simpler model; no per-agent key.
- *Con*: loses cross-process verification. The
  `agent_id` is the natural identity. **Rejected**
  for v1; a v2 "operator-key" mode can complement.

### 5.6 Merkle tree (STH) without per-event signing

- *Pro*: smaller overhead; one signature per
  1000 events.
- *Con*: a single STH verifies a **batch**; a
  per-event verification is more granular. v1
  signs per-event; v2 adds STH as a complementary
  layer. **Rejected as primary**; reserved for v2.

### 5.7 No signing (status quo + access controls)

- *Pro*: zero overhead; zero new deps.
- *Con*: does not address the threat model in §3.3.
  Access controls (Redis ACL) are necessary but
  insufficient: a misconfigured exporter or a
  compromised dependency bypasses ACL. **Rejected**.

---

## 6. Consequences

### 6.1 Positive

- **Tamper-evidence at the event level**: a single
  modified byte breaks the signature. The
  `EventLog` dedup by `event_id` already catches
  content-level mutations; the signature catches
  *identity* forgery (an event with the right
  `event_id` but the wrong producer).
- **Algorithm agility**: migration to a new
  algorithm is a versioned field, not a breaking
  change.
- **Cross-process verification**: the `ProcessLearnerSystem`
  (fmh_office) can verify the terminal event's
  signature against the engine's public key
  before persisting to FalkorDB. The FalkorDB
  graph becomes a verifiable audit trail.
- **Cross-implementation readiness**: the JCS
  canonical form is implementable in any language
  with a RFC 8785 library. The signature payload
  (alg, pk, sig) is a small string map.

### 6.2 Negative

- **Performance overhead**: ~30-50µs per event
  (sign) + ~10-20µs (verify) on the canonical
  Python implementation. For the `fmh_office` MVP
  (5 events/pedido, < 1.5s end-to-end) this is
  ~200µs — negligible. For high-throughput
  pipelines (> 10k events/s/agent) the overhead
  becomes ~1% of the event loop; PR 1 will
  benchmark and PR 1.1 will add an "unsigned" fast
  path for inner-loop use.
- **Dependency surface**: `cryptography` (PyCA,
  ~30 MB wheel) and `canonicaljson` (~50 KB).
  Both are stable, audited, and widely used.
- **Key management is in-process**: a process
  restart loses the keys unless the application
  persists them. v1 documents this; v2 brings
  Vault/KMS.
- **Ed25519 is not quantum-safe**: the FIPS / NSA
  suite is on a 10-15 year migration timeline. v3
  brings ML-DSA.

### 6.3 Neutral

- **Wire format change**: additive, non-breaking.
  Existing events load fine; new events carry
  `signature`. The `EventLog.append` signature
  schema change is **bump**: existing keys continue
  to work because we use `redis.xadd` with no
  explicit schema (Redis Streams are schemaless).
- **Tests grow by ~15-20 unit tests + 3-5
  integration tests**: total framework test
  count goes from ~700 to ~720.

---

## 7. Rollout

### 7.1 PRs

| PR | Scope | LoC | Risk |
|---|---|---|---|
| 0 | `security/` skeleton: `KeyRegistry` Protocol, `InMemoryKeyRegistry`, JCS helper, tests with stdlib stubs. No `Event` change. | ~400 | low |
| 1 | `Signature` + `sign_event` / `verify_event` (Ed25519 via `cryptography`). | ~200 | low |
| 2 | `Event.signature: Optional[Signature]` field, `to_dict` / `from_dict` roundtrip. | ~150 | low |
| 3 | `EventLog` wire format: `signature` field in `_event_to_redis` and `_parse_event`. | ~100 | medium (Redis schema) |
| 4 | `AggregateSignature` (`concat-v1`) + `verify_aggregate_concat`. | ~200 | low |
| 5 | `EventLog.require_signatures: bool = False` opt-in enforcement. | ~150 | medium |
| 6 | `fmh_office.learning.system`: optional signing of the terminal event before projection. | ~80 | low |
| 7 | Docs: `fmh_backend/docs/security/signing.md` + `fmh_office/docs/security/signing.md`. | ~300 | low |
| 8 | This ADR (final). | — | — |

**Total**: ~1580 LoC code + tests + ~300 LoC docs.

### 7.2 Migration path for existing events

- Pre-ADR events: `signature=None`. `verify_event`
  returns `False` for them; consumers that opt in
  see them as "unverified" and may log a warning
  or skip.
- A v1.1 utility (`resign_old_events.py`) reads
  each agent's `EventLog`, re-emits each event with
  a fresh signature under the current key, and
  appends a `:resigned` marker. **Out of scope for
  this ADR.**
- Rotation: when an `agent_id`'s key rotates, old
  events signed under the old key continue to verify
  (the verifier stores multiple public keys per
  `agent_id`). A v2 utility `rotate_keys.py`
  handles the dual-key period. **Out of scope for
  this ADR.**

### 7.3 v2 / v3 roadmap

- **v1.1**: re-sign old events utility.
- **v1.2**: `Ed25519ph` for > 1 KB payloads.
- **v2.0**: Vault / KMS / HSM `KeyRegistry`
  implementations; `ecdsa-p256-sha256-v1` algorithm;
  STH (Merkle tree per-agent); key rotation utility.
- **v2.1**: BLS12-381 aggregate (`alg:
  "bls12-381-v1"`).
- **v3.0**: ML-DSA-65 (post-quantum); hash-then-sign
  for very large payloads.

---

## 8. Open questions

- **Should `require_signatures=True` reject events
  with `signature=None`, or log a warning and
  accept?** v1 ships `reject`. v2 may add
  `warn_only=True` for migration windows.
- **Where does the public key live for verification
  by an external consumer (e.g. a dashboard)?** v1:
  the consumer queries the producer's process
  (via a new `GET /agents/{id}/public_key` endpoint
  in `mvp.http`). v2: a public key registry
  (Vault KV, separate Redis key).
- **Algorithm negotiation in HTTP**: if a future
  client wants to sign with `ecdsa-p256-sha256-v1`
  and the server only knows `ed25519-v1`, do we
  fail the request with 400, or do we accept and
  reject at signature-verify time? v1: 400 (the
  server advertises its algorithm in
  `OPTIONS /agents/{id}/capabilities`). v2: a
  richer negotiation.

These are explicitly out of scope for v1. They are
documented here so the v2 ADR has continuity.

---

## 9. References

- **RFC 8032** — *Edwards-Curve Digital Signature
  Algorithm (EdDSA)* — IETF, 2017.
- **RFC 8785** — *JSON Canonicalization Scheme
  (JCS)* — IETF, 2020.
- **RFC 6979** — *Deterministic Usage of DSA and
  ECDSA* — IETF, 2013.
- **NIST FIPS 186-5** — *Digital Signature
  Standard* — NIST, 2023.
- **draft-irtf-cfrg-bls-signature-04** — *BLS
  Signatures* — IETF IRTF CFRG (not yet RFC).
- **RFC 9162** — *Certificate Transparency v2* — for
  the STH pattern (v2 of this ADR).
- **`fmh_backend/src/fmh_backend/core/event.py`**
  — the `Event` class; lines 256-541.
- **`fmh_backend/src/fmh_backend/stream/event_log.py`**
  — the wire format; line 269.
- **`fmh_backend/src/fmh_backend/security/`** —
  new package; the `Signature`, `AggregateSignature`,
  `KeyRegistry`, and `canonical_event_bytes` live
  here.
- **`cryptography` (PyCA)** — `Ed25519PrivateKey.sign`
  / `Ed25519PublicKey.verify`.
- **`canonicaljson` (cyberphone)** — RFC 8785
  reference impl in Python.
- **ADR-001 (Architecture)**, **ADR-002 (Replay)**,
  **ADR-005 (Idempotency)**, **ADR-012
  (IntentRouter)**, **ADR-015 (fmh_office vertical)**.

---

## 10. Decision

**Adopted.** Implementation proceeds per §7
(PRs 0-8, ~1580 LoC, 1-2 weeks).
