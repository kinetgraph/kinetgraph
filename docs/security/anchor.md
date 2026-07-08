<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Tamper-Evidence and Continuous Verification (Level 3)

L1 proves **who** emitted an event. L2 proves **what** they
were allowed to emit. L3 proves the **historical event log
has not been retroactively edited** — even by an attacker who
later compromises the producer's signing key.

> **Status**: proposed (ADR-017). Code lives in PRs ZT-3.1 to
> ZT-3.10 of the zero-trust rollout.
>
> **Depends on**: [signing.md](./signing.md) (L1) and
> [authorization.md](./authorization.md) (L2).

---

## 1. What you get

After enabling L3:

- A `VerificationCache` (LRU+TTL) reduces verify cost to
  ~1µs/event on cache hit while keeping veredito **fresh**
  (configurable TTL, default 60s).
- A per-agent **Merkle-style hash-chain anchor** is signed
  every N events (default 100) or every T seconds (default
  300s), using a **long-term key** separate from the per-event
  signing key.
- A retro-editing detector walks the EventLog + anchor chain
  and quarantines any divergence (an event whose bytes
  changed after the anchor was signed).
- An **audit API** (`GET /agents/{id}/anchors`) lets an
  external auditor replay the chain independently.

---

## 2. Concepts

### 2.1 Verification cache

```python
# fmh_backend/src/fmh_backend/security/verification_cache.py
@dataclass(frozen=True, slots=True)
class CacheEntry:
    event_id: UUID
    pubkey_fingerprint: str   # sha256(pk)[:16]
    policy_hash: str          # sha256(policy)[:16]
    verified_at: float        # unix_ms
    verdict: bool

class VerificationCache(Protocol):
    def get(self, event_id: UUID) -> Optional[CacheEntry]: ...
    def put(self, entry: CacheEntry, ttl_s: float) -> None: ...
```

Cache hit short-circuits `verify_event` to a hash-table lookup
(~500ns). Cache miss falls through to the full cryptographic
verify path.

**Freshness**: a cached verdict is valid for `ttl_s`. After
that, the entry is re-verified. The cache is **not** an
authoritative source — it is a perf optimisation.

**Invalidation events**:
- Key revoked → cache flush for that `agent_id`.
- Policy changed → cache flush for that `agent_id`.
- Manual `cache.invalidate(event_id)`.

### 2.2 Hash-chain anchor

```python
# fmh_backend/src/fmh_backend/security/anchor.py
@dataclass(frozen=True, slots=True)
class Anchor:
    agent_id: str
    epoch: int                # monotonic per agent_id
    range_start: int          # stream index of first event in this anchor
    range_end: int            # stream index of last event in this anchor
    chain_hash: str           # sha256(prev_chain || canonical_event_bytes(e_i))
    signature: Signature      # signed by long_term_key
    created_at: datetime
```

The anchor is a **self-certifying** chain: each anchor
includes the previous anchor's `chain_hash`, creating a
tamper-evident sequence. The `long_term_key` is rotated
quarterly (rotation script in §6); events signed under old
anchor keys continue to verify (auditor holds the historical
keys).

### 2.3 `AnchorScheduler`

A background task that fires every `interval_s` (default 300s)
or every `events_per_anchor` events (default 100), whichever
comes first:

```
if (now - last_anchor_at) >= interval_s
   or (events_since_last_anchor) >= events_per_anchor:
       compute_anchor()
       sign_anchor()
       XADD fmh:anchors:{agent_id} anchor_dict
```

The scheduler is per-agent. A single FMH process can schedule
hundreds of agents; the cost is amortised.

### 2.4 Retro-editing detector

```python
# fmh_backend/src/fmh_backend/security/detector.py
class RetroEditingDetector:
    """Walks EventLog + anchor chain; reports divergences."""

    async def scan(
        self,
        agent_id: str,
        from_epoch: int = 0,
    ) -> list[Divergence]:
        ...
```

A `Divergence` carries:

- `event_id`: the event whose bytes differ from anchor.
- `stream_id`: the Redis Stream entry that was modified.
- `anchor_epoch`: the anchor that should have witnessed the
  original bytes.
- `expected_canonical_bytes`: what the anchor committed to.
- `actual_canonical_bytes`: what's in the Stream now.

The detector does NOT auto-quarantine; it returns the list and
the operator decides (audit dashboard, alert webhook,
auto-DLQ).

---

## 3. Wiring through `EventLog`

### 3.1 Configuration

```python
from kntgraph.security.verification_cache import LruVerificationCache
from kntgraph.security.anchor import (
    AnchorScheduler,
    HashChainAnchor,
)
from kntgraph.security.detector import RetroEditingDetector

cache = LruVerificationCache(max_size=10_000, default_ttl_s=60)
anchor = HashChainAnchor(long_term_key_registry=long_term_registry)
scheduler = AnchorScheduler(
    event_log=log,
    anchor=anchor,
    interval_s=300,
    events_per_anchor=100,
)
detector = RetroEditingDetector(event_log=log, anchor=anchor)

log = EventLog(
    redis,
    key_registry=key_registry,
    policy_registry=policy_registry,
    verification_cache=cache,
    require_signatures=True,
)

# Start background scheduler
await scheduler.start()
```

### 3.2 Append path (L1 + L2 + L3)

```
EventLog.append(event)
    │
    ├─► cache.get(event.event_id)         (L3 fast path)
    │     ├─► hit & fresh & verdict=true  → return cached stream_id
    │     └─► miss / stale                 → fall through
    │
    ├─► signature verify                  (L1)
    │     └─► cache.put(verdict, ttl)
    │
    ├─► policy check                      (L2)
    │
    └─► XADD fmh:agents:{agent_id}:events
            │
            └─► scheduler.notify(event_count++)   (L3 background)
```

L3 does NOT add to the hot path: the cache is the fast path,
and the scheduler is a background task. The `events_per_anchor`
counter is incremented in-process; `interval_s` is checked on
each append tick.

### 3.3 HTTP audit API

```python
# fmh_office/src/fmh_office/mvp/http.py (extension)
@app.get("/agents/{agent_id}/anchors")
async def get_anchors(
    agent_id: str,
    from_epoch: int = 0,
    to_epoch: Optional[int] = None,
    api_key: str = Depends(verify_audit_api_key),
):
    return await anchor_store.read_range(
        agent_id=agent_id,
        from_epoch=from_epoch,
        to_epoch=to_epoch,
    )
```

Response:

```json
{
  "agent_id": "session-42",
  "anchors": [
    {
      "epoch": 0,
      "range_start": 1,
      "range_end": 100,
      "chain_hash": "sha256:...",
      "signature": {"alg": "ed25519-v1", "pk": "...",
                    "sig": "...", "key_epoch": 0},
      "created_at": "2026-06-22T10:00:00Z"
    },
    {
      "epoch": 1,
      "range_start": 101,
      "range_end": 200,
      "chain_hash": "sha256:...",
      "signature": {...},
      "created_at": "2026-06-22T10:05:00Z"
    }
  ]
}
```

The auditor verifies the chain offline: for each anchor,
verify the signature; recompute `chain_hash` from the events
between `range_start` and `range_end`; assert it matches.

---

## 4. Worked example: detecting a retroactive edit

```python
import asyncio
import redis.asyncio as aioredis
from kntgraph.security.anchor import (
    HashChainAnchor,
    AnchorScheduler,
)
from kntgraph.security.detector import RetroEditingDetector

async def main():
    redis = aioredis.from_url("redis://localhost:6379")
    long_term_registry = InMemoryKeyRegistry()
    long_term_priv, _ = generate_keypair()
    long_term_registry.register(
        agent_id="anchor:session-42", priv=long_term_priv,
    )

    anchor = HashChainAnchor(long_term_registry)
    log = EventLog(redis, key_registry=key_registry)
    scheduler = AnchorScheduler(log, anchor, events_per_anchor=10)
    await scheduler.start()

    # Emit 20 events
    for i in range(20):
        e = build_event(...)
        await log.append(e)

    # Force an anchor now (don't wait for interval)
    await scheduler.run_once()

    # Attacker edits event #15's data field in the Stream
    stream_key = "fmh:agents:session-42:events"
    # ... redis-cli XADD with modified fields ...

    # Detector finds the divergence
    detector = RetroEditingDetector(log, anchor)
    divergences = await detector.scan("session-42")
    for d in divergences:
        print(f"DIVERGENCE: event_id={d.event_id}")
        print(f"  expected: {d.expected_canonical_bytes[:80]}")
        print(f"  actual:   {d.actual_canonical_bytes[:80]}")

    await redis.aclose()

asyncio.run(main())
```

Output:

```
DIVERGENCE: event_id=7c2a-...
  expected: sha256:c3d4...
  actual:   sha256:f9e8...
```

The auditor investigates; the divergence is either a legitimate
schema migration (rare, requires coordination with the auditor)
or an attack.

---

## 5. Long-term key rotation

The `long_term_key` signs anchors. It is **separate** from
the per-event signing key (L1) because:

- Per-event keys rotate more frequently (operator policy).
- Anchor keys rotate quarterly (low frequency, high
  durability requirement).
- Compromise of an anchor key is a **worse** failure mode
  than compromise of a per-event key (the anchor key
  witnesses history; a per-event key only authenticates one
  event).

### 5.1 Rotation procedure

```bash
# 1. Generate new long-term key
python -m fmh_backend.security.scripts.rotate_long_term_key \
    --agent-id "anchor:session-42" \
    --new-key-out /etc/fmh/keys/anchor-session-42-2026Q3.pem

# 2. Distribute new key to all verifiers (Vault KV write,
#    config push, etc.)

# 3. Update KeyRegistry at next boot (or hot-reload)

# 4. Old anchor key remains valid for historical anchors;
#    the verifier holds the key history
#    (KeyRegistry: list of (epoch, pubkey, retired_at))
```

### 5.2 Verifier key history

```python
class KeyRegistry(Protocol):
    def public_key(
        self,
        agent_id: str,
        key_epoch: int = 0,
    ) -> PublicKey:
        """Return the pubkey active at key_epoch.
        Searches retired keys if not in current."""

    def retired_keys(
        self,
        agent_id: str,
    ) -> list[tuple[int, PublicKey, datetime]]:
        """List all retired keys with retirement timestamps."""
```

The verifier does **not** reject anchors signed under retired
keys; it verifies against the key that was active at the
anchor's `created_at` time. This is the difference between
**revocation** (L2: reject future events) and **rotation**
(L3: keep history verifiable).

---

## 6. Performance

| Operation | Median | p99 |
|---|---|---|
| Cache hit | 500ns | 2µs |
| Cache miss → full verify | 35µs | 50µs |
| Anchor compute (100 events) | 4ms | 8ms |
| Anchor sign (Ed25519) | 30µs | 45µs |
| Detector scan (1000 events) | 50ms | 120ms |

L3 adds ~500ns on cache hit (the dominant case) and ~4ms every
100 events (anchor compute, amortised). The detector is
offline (audit runs); not in the hot path.

---

## 7. Operational concerns

### 7.1 What happens if Redis is wiped

If an attacker has the access to wipe `fmh:agents:{id}:events`,
the detector will report **all** events as missing (anchor
chain references them, stream doesn't have them). This is
**detectable**, not preventable. Recovery: re-emit from
backup + re-anchor (manual process; documented in runbook).

### 7.2 What if the long-term key is lost

**All anchors become unverifiable.** This is the worst-case
failure mode. Mitigation:

- HSM/KMS (L4) with quorum recovery.
- Periodic anchor-key backup to offline storage.
- Multiple verifiers hold copies (audit redundancy).

### 7.3 What if anchors are never written

The scheduler must be running. If the FMH process dies and
restarts, the scheduler resumes from `last_anchor_epoch` (in
the long-term registry's metadata). A liveness check should
alert if `now - latest_anchor.created_at > 2 * interval_s`.

### 7.4 False positives in the detector

Schema migrations, time-zone changes in `timestamp`, and
formatting changes in `data` produce divergences. Best
practice: do schema migrations in **separate event types**
(`event_type="schema.v2_migration"`) and never edit
historical `data` fields.

---

## 8. Worked example: end-to-end audit

```bash
# 1. Auditor fetches the anchor chain
curl -H "X-Audit-API-Key: ..." \
    "https://fmh.example.com/agents/session-42/anchors" \
    > anchors.json

# 2. Auditor fetches the EventLog dump
redis-cli --no-raw XRANGE \
    fmh:agents:session-42:events - + COUNT 1000 \
    > stream.json

# 3. Offline verification (Python script)
python scripts/verify_anchors.py \
    --anchors anchors.json \
    --stream stream.json \
    --pubkey /etc/fmh/keys/anchor-session-42-pub.pem
```

`verify_anchors.py`:

```python
def verify_chain(anchors, stream_entries, pubkey):
    prev_chain = b""
    for anchor in anchors:
        # Verify signature
        canonical_anchor = canonical_event_bytes(anchor)
        if not verify_signature(canonical_anchor, anchor.signature, pubkey):
            return False, f"signature failed at epoch {anchor.epoch}"

        # Recompute chain hash
        events_in_range = stream_entries[
            anchor.range_start : anchor.range_end + 1
        ]
        chain_hash = prev_chain
        for entry in events_in_range:
            event = parse_event(entry)
            chain_hash = sha256(chain_hash + canonical_event_bytes(event))

        if chain_hash.hex() != anchor.chain_hash:
            return False, f"chain mismatch at epoch {anchor.epoch}"

        prev_chain = bytes.fromhex(anchor.chain_hash)

    return True, "all anchors verify"
```

---

## 9. Common pitfalls

### 9.1 Anchoring too frequently
Anchoring every event is wasteful (~30µs/event for the sign
+ 35µs for the canonical bytes; 10k events = 350ms+ in the
hot path). The default of `events_per_anchor=100` is the
sweet spot. Lower it for high-value event types; raise it
for high-volume low-value types.

### 9.2 Anchoring too infrequently
If the anchor window is too large, the cost of retroactive
editing before detection is high. For audit-critical
pipelines, anchor every 10 events; for throughput-critical
pipelines, anchor every 1000.

### 9.3 Caching with wrong TTL
`ttl_s=0` (no caching) is correct but slow. `ttl_s=∞`
defeats the "freshness" property — the cache can serve a
verdict based on a key that has since been revoked. Default
60s; tune to your revocation propagation SLA.

### 9.4 Ignoring the detector
The detector is **offline by default**. If you don't run it
periodically, retro-editing goes unnoticed. Best practice:
run it daily (cron), alert on any non-empty result.

### 9.5 Confusing revocation with rotation
- **Revoke** (L2): reject future events under this key. Old
  events still verify.
- **Rotate** (L3): phase out the long-term anchor key. Old
  anchors still verify (key history is held).
- **Retire**: equivalent to rotate for a per-event key.

---

## 10. Testing your integration

```python
def test_cache_hit_returns_cached_verdict():
    """Second verify with same event_id is fast (< 5µs)."""

def test_cache_ttl_expiry_reverifies():
    """After TTL, entry is re-verified (fresh verdict)."""

def test_anchor_chain_verifies_end_to_end():
    """10 events → 1 anchor → verify chain_hash from events."""

def test_retroactive_edit_detected():
    """Mutate event in Stream after anchor; detector flags it."""

def test_long_term_key_rotation_preserves_history():
    """Old anchor signed under rotated key still verifies."""

def test_audit_api_returns_canonical_chain():
    """GET /agents/{id}/anchors returns JCS-canonical JSON."""
```

---

## 11. Migration from L2

1. **Deploy with `interval_s=86400, events_per_anchor=10000`**.
   Anchors daily, never more often. Verify nothing breaks.
2. **Tighten to `interval_s=3600`** after a week of stability.
3. **Tighten to `interval_s=300, events_per_anchor=100`**
   (the default) once monitoring confirms the scheduler is
   healthy.
4. **Set up the detector cron.** Daily run; alert on any
   divergence.
5. **Document the long-term key rotation runbook.** Test
   rotation in staging quarterly.
6. **Expose the audit API** to the security team (read-only
   credentials).

---

## 12. See also

- [signing.md](./signing.md) — Level 1 (authentication)
- [authorization.md](./authorization.md) — Level 2 (RBAC)
- [threat_model.md](./threat_model.md) — formal threat model
- [README.md](./README.md) — overview of all levels
- [ADR-016](../../ADRs/ADR-016-Event-Signing.md) — L1 design
- ADR-017 (proposed) — L3 design
