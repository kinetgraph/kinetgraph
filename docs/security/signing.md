<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Event Signing (Level 1: Authenticated Producers)

This document covers the **operational** side of ADR-016:
how to sign events, verify them, wire them through `EventLog`,
and what to expect at runtime. For the design rationale
(algorithm choice, threat model, alternatives), read
[ADR-016](../../ADRs/ADR-016-Event-Signing.md) directly.

> **Status**: proposed (code lives in PRs 0-8 of ADR-016).
> **Audience**: anyone integrating the Kinetgraph with multi-tenant
> Redis or any deployment where event authenticity matters.

---

## 1. What you get

After enabling L1:

- Each `Event` carries an optional `Signature` (Ed25519 over
  JCS canonical bytes of `to_dict()`).
- `EventLog.append` verifies the signature when present.
- `EventLog.require_signatures=True` rejects unsigned events.
- Cross-implementation: signatures computed in Python 3.10
  verify in 3.11, 3.12, and any other language with an RFC 8785
  library.
- Algorithm agility: `alg: "ed25519-v1"` is the v1 default;
  future algorithms land as `alg: "<name>-v<n>"`.

---

## 2. Concepts

### 2.1 The `Signature` dataclass

```python
# src/kntgraph/security/signing.py
@dataclass(frozen=True, slots=True)
class Signature:
    alg: str          # "ed25519-v1"
    pk: str           # base64 of 32-byte Ed25519 public key
    sig: str          # base64 of 64-byte Ed25519 signature
    key_epoch: int = 0  # monotonic per agent_id (L2 concept)
```

The `Signature` is added to `Event` as `Optional`:

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

The `event_id` UUID5 **does not change**. Signing is additive;
dedup by `event_id` continues to work.

### 2.2 Canonical bytes (RFC 8785 JCS)

```python
from kntgraph.security.canonical import canonical_event_bytes

# Serialize an event (with signature stripped) to RFC 8785 bytes
bytes_to_sign: bytes = canonical_event_bytes(event)
```

`canonical_event_bytes` is **deterministic** across:

- Python versions (3.10, 3.11, 3.12).
- Platforms (Linux, macOS, Windows).
- Future language clients (Go, Rust, Java with a JCS impl).

This is the **only** bytes representation of an event that is
stable. The existing `Event.to_json` (using `json.dumps(...)`)
is **not** canonical and should not be used for signing.

### 2.3 Sign / verify

```python
from kntgraph.security.signing import sign_event, verify_event

# Producer side
key = registry.private_key(agent_id="session-42")  # InMemoryKeyRegistry
signed_event = sign_event(event, key)

# Consumer side
pub = registry.public_key(
    agent_id="session-42",
    key_epoch=signed_event.signature.key_epoch,
)
ok: bool = verify_event(signed_event, pub)
```

`verify_event` returns `False` on:

- Unknown `alg` (algorithm whitelist).
- Wrong pubkey, wrong bytes, wrong epoch, revoked key.
- Corrupted signature bytes.

`verify_event` **never raises**. Use the boolean directly.

### 2.4 KeyRegistry (in-process v1)

```python
from kntgraph.security.keys import (
    InMemoryKeyRegistry,
    generate_keypair,
)

registry = InMemoryKeyRegistry()
priv, pub = generate_keypair()
registry.register(agent_id="session-42", priv=priv)

# v2: VaultKeyRegistry / KmsKeyRegistry (HSM)
# v1 limitation: keys lost on process restart.
# Mitigation: persist PEM at boot (10-line utility).
```

The `KeyRegistry` is a `Protocol`. Production code accepts the
Protocol; tests use `InMemoryKeyRegistry`. v2 plugs in
Vault / KMS without changing call sites.

### 2.5 Loading keys from Environment Variables (v1 Mitigation)

Since the `InMemoryKeyRegistry` loses keys on process restart, a common mitigation for v1 deployments is to inject the private key via an environment variable. You can use the standard `cryptography` library to parse the PEM string and wrap it for Kinetgraph:

```python
import os
from cryptography.hazmat.primitives import serialization
from kntgraph.security.keys import InMemoryKeyRegistry, Ed25519PrivateKeyWrapper

def load_key_from_env(agent_id: str, env_var_name: str, registry: InMemoryKeyRegistry):
    pem_data = os.environ.get(env_var_name)
    if not pem_data:
        raise ValueError(f"Key not found in env var {env_var_name}")

    # Parse the PEM into an Ed25519PrivateKey
    raw_key = serialization.load_pem_private_key(
        pem_data.encode("utf-8"),
        password=None  # or provide a password if encrypted
    )
    
    # Wrap it for the registry
    priv_wrapper = Ed25519PrivateKeyWrapper(_key=raw_key, algorithm="ed25519-v1")
    registry.register(agent_id, priv=priv_wrapper)

# Example usage:
# registry = InMemoryKeyRegistry()
# load_key_from_env("session-42", "MY_SECRET_AGENT_KEY", registry)
```

---

## 3. Wiring through `EventLog`

### 3.1 Producer side

```python
import redis.asyncio as aioredis
from kntgraph.stream.event_log import EventLog
from kntgraph.security.keys import InMemoryKeyRegistry

async def main():
    redis = aioredis.from_url("redis://localhost:6379")
    registry = InMemoryKeyRegistry()
    registry.register("session-42", priv=...)

    log = EventLog(redis)  # L0: no enforcement

    e = build_event(...)
    signed = sign_event(e, registry.private_key("session-42"))
    await log.append(signed)  # roundtrips signature through Redis
```

### 3.2 Consumer side

```python
async def consumer_loop():
    redis = aioredis.from_url("redis://localhost:6379")
    registry = InMemoryKeyRegistry()
    registry.load_pem(...)  # boot: hydrate from disk / vault

    log = EventLog(redis, registry=registry)

    async for event in log.read("session-42"):
        if event.signature is None:
            log.warning("unsigned event", event_id=event.event_id)
            continue
        if not verify_event(event, registry.public_key(
            event.agent_id,
            key_epoch=event.signature.key_epoch,
        )):
            log.error(
                "signature mismatch",
                event_id=event.event_id,
                agent_id=event.agent_id,
            )
            await dlq.enqueue(event, reason="signature_invalid")
            continue
        await process(event)
```

### 3.3 Enforce at append

```python
log = EventLog(
    redis,
    key_registry=registry,
    require_signatures=True,    # reject event.signature is None
    signature_warn_only=False,  # raise vs warn (default: raise)
)
```

When `require_signatures=True`:

| Input | Behaviour |
|---|---|
| `event.signature is None` | `UnsignedEventRejected` raised |
| `event.signature.alg` unknown | `UnknownAlgorithmRejected` raised |
| `verify_event(...) is False` | `SignatureVerificationFailed` raised |
| OK | Event appended |

`signature_warn_only=True` softens the first three rows to a
warning log; useful during rollout.

### 3.4 Wire format (Redis Stream)

`EventLog._event_to_redis` adds a `signature` key (JSON-encoded
`Signature` dict, or `""` when absent). Pre-L1 readers ignore
the extra field (Redis Streams are schemaless). `from_dict` /
`_parse_event` decode the field back; absence → `signature=None`.

This is **additive**. Pre-L1 events in Redis continue to load
with `signature=None`.

---

## 4. Worked example: end-to-end

```python
import asyncio
import redis.asyncio as aioredis
from kntgraph.core.event import Event, EventClass
from kntgraph.stream.event_log import EventLog
from kntgraph.security.keys import (
    InMemoryKeyRegistry,
    generate_keypair,
)
from kntgraph.security.signing import sign_event, verify_event

async def main():
    redis = aioredis.from_url("redis://localhost:6379")

    # Producer: build key registry + event log
    producer_registry = InMemoryKeyRegistry()
    priv, pub = generate_keypair()
    producer_registry.register("session-42", priv=priv)

    producer_log = EventLog(redis, require_signatures=True)

    # Build + sign + append
    e = Event.create(
        event_type="pedido.received",
        agent_id="session-42",
        event_class=EventClass.DOMAIN,
        data={"cliente_id": "cli-001", "valor_total": 100.0},
    )
    signed = sign_event(e, producer_registry.private_key("session-42"))
    await producer_log.append(signed)

    # Consumer: separate process, separate registry
    consumer_registry = InMemoryKeyRegistry()
    consumer_registry.register("session-42", priv=priv)  # same key
    consumer_log = EventLog(redis, key_registry=consumer_registry)

    async for read_event in consumer_log.read("session-42"):
        ok = verify_event(read_event, consumer_registry.public_key(
            "session-42",
            key_epoch=read_event.signature.key_epoch,
        ))
        assert ok, "signature must verify"

        print(f"{read_event.event_type} {read_event.event_id}")
        # pedido.received 7c2a...

    await redis.aclose()

asyncio.run(main())
```

---

## 5. Performance

Measured on a modern CPU (`tests/perf/bench_sign.py`):

| Operation | Median | p99 |
|---|---|---|
| `sign_event` (Ed25519 over ~500B event) | 28µs | 41µs |
| `verify_event` | 9µs | 14µs |
| `canonical_event_bytes` (JCS) | 35µs | 52µs |
| **Combined sign + canonicalise** | 63µs | 93µs |

For the kinetgraph MVP (5 events/pedido), end-to-end signing adds
~300µs to a ~1.5s pipeline. **Negligible.**

For high-throughput pipelines (> 10k events/s/agent), PR 1.1 adds
an "unsigned fast path" with `Event.log_unsigned` for inner loops
where signing is not needed.

---

## 6. Algorithm agility

The `Signature.alg` field is the version contract:

| `alg` value | Status | Use |
|---|---|---|
| `ed25519-v1` | shipped (v1) | Default for new deployments |
| `ecdsa-p256-sha256-v1` | v2 | FIPS-only deployments |
| `bls12-381-v1` | v2.1 | Aggregation use cases |
| `ml-dsa-65-v1` | v3 | Post-quantum migration |

A verifier that does not know the algorithm returns `False`
(NOT exception). A producer that tries to sign with an unknown
algorithm is rejected at signature creation
(`UnknownAlgorithmRejected`).

This makes migration safe: deploy the new algorithm producer
alongside the old; old verifiers reject the new sigs as
"unknown alg" — which is **expected** during a migration window.

---

## 7. Common pitfalls

### 7.1 Don't sign the signature
The canonical bytes **omit** the `signature` field itself. The
helper `canonical_event_bytes(event)` does this automatically.
Manually doing `json.dumps(event.to_dict())` would include
`signature=None` and produce a different byte sequence.

### 7.2 Don't mix `to_json` and JCS
`Event.to_json` is **not** RFC 8785. It uses
`json.dumps(..., default=str)` which produces different bytes
across Python versions. The signature helper uses JCS; never
compute signatures from `to_json` bytes.

### 7.3 Don't reuse keys across agents
Each `agent_id` gets its own keypair. Sharing keys across
`agent_id`s defeats the purpose: an attacker who compromises one
agent's secret can emit events as the other.

### 7.4 Don't store private keys in `data`
The `Event.data` field is in clear in Redis. **Never** put
private keys, API tokens, or PII (raw CPF, names, addresses)
in `data`. Use `SecureComponent` (ADR-014) for confidential
payloads; hash PII (sha256[:16] + `sha256:` prefix).

### 7.5 Don't rely on signature for replay protection
Signing proves identity, not freshness. Replay protection comes
from `event_id` dedup (UUID5 over payload). For stronger
freshness, the L2 rate-limit (`max_event_rate_per_sec`) bounds
the volume of events a compromised agent can re-inject.

---

## 8. Testing your integration

### 8.1 Unit tests you should write

```python
def test_my_event_round_trips_through_event_log():
    """After sign → append → read, signature still verifies."""

def test_unsigned_event_rejected_when_required():
    """EventLog(require_signatures=True) rejects signature=None."""

def test_wrong_key_fails_verification():
    """Signing with key A, verifying with pubkey B returns False."""

def test_unknown_alg_fails_verification():
    """Signature with alg='unknown-v9' fails verify_event gracefully."""

def test_canonical_bytes_stable_across_dict_order():
    """Two events with same data but different dict order yield same bytes."""
```

### 8.2 Cross-implementation test (CI)

```yaml
# .github/workflows/sign-interop.yml
strategy:
  matrix:
    python: ["3.10", "3.11", "3.12"]
steps:
  - run: pytest tests/integration/test_signing_interop.py
```

`tests/integration/test_signing_interop.py` signs in 3.10,
verifies in 3.11, verifies in 3.12. Must pass in all three.

---

## 9. Migration from L0

Three steps:

1. **Deploy L1 producer.** Sign events; do not enforce.
   `EventLog(require_signatures=False)` continues to accept all
   events. Old events load with `signature=None`.

2. **Verify consumers.** As consumers roll out, they can
   enable `verify_event` on read. Old events log a warning;
   new events must verify.

3. **Enable enforcement.** `EventLog(require_signatures=True,
   signature_warn_only=True)` for one release window; then
   `signature_warn_only=False`.

The `scripts/resign_old_events.py` utility (v1.1) walks the
EventLog and re-signs old events under the current key. Not in
v1.

---

## 10. See also

- [ADR-016](../../ADRs/ADR-016-Event-Signing.md) — design record
- [README.md](./README.md) — overview of all levels
- [authorization.md](./authorization.md) — Level 2 (RBAC)
- [anchor.md](./anchor.md) — Level 3 (tamper evidence)
- [threat_model.md](./threat_model.md) — formal threat model
