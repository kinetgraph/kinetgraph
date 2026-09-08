<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Authorization (Level 2: Authorised Producers)

L1 (signing) proves **who** emitted an event. L2 proves **what
they were allowed to emit**. Without L2, an authenticated agent
can sign events of any `event_type` — including types it should
not have access to (e.g. `process.cancelled` from a role that
should only emit `pedido.received`).

> **Status**: proposed (ADR-017a). Code lives in PRs ZT-2.1 to
> ZT-2.9 of the zero-trust rollout.
>
> **Depends on**: [signing.md](./signing.md) (L1).

---

## 0. The two real gates (tool authorization)

> **Status**: implemented. This is the authorization model that
> actually ships in the framework today — the `CapabilityPolicy`
> below (§1–§11) is the *proposed* L2 extension. The two gates
> below are the **real, enforced** path for tool access.

The framework authorises a tool call through **two sequential,
independent gates** (ADR-060 §3.0, ADR-066 §4.1). A request must
clear both; failing either blocks the call. The two gates answer
two different questions:

| Gate | Owner | Lives in | Persists? | What it answers |
|---|---|---|---|---|
| **1. RBAC of the request** | `PrincipalLevel` on the inbound `Principal` | `ToolACL.check(principal)` in `WorkerManager` | No (`producer_principal_id` on the event) | "Does **this request** have the infrastructure-level permission to invoke this tool?" |
| **2. Persona of the agent** | `RoleComponent` on the destination `AgentView` | `has_tool_access(role, system)` in the business system | Yes (fold) | "Is **the agent that will execute** dressed with a persona that admits the system?" |

The two gates are **independent**: a higher RBAC level on the
request does **not** bypass a persona that forbids the system,
and a persona that admits the system does **not** bypass a
tenant-pinned ACL.

### 0.1 Gate 1 — RBAC of the request (`WorkerManager`)

Each registered tool carries an optional `ToolACL`
(`kntgraph.tools.acl`). `WorkerManager.register(tool_cls, acl=...)`
attaches it; `WorkerManager._process_message` calls
`acl.check(principal)` **before** consuming a worker slot. On
denial it emits `tool.<name>.failed` with reason `acl_denied`
and acks the message — the request fails fast without running
the worker.

```python
from kntgraph.tools import WorkerManager, ToolACL
from kntgraph.security import PrincipalLevel

worker_manager = WorkerManager(redis=..., event_log=...)
worker_manager.register(
    MockChatLlmWorker,
    acl=ToolACL(
        required_level=PrincipalLevel.agent,
        tenant_pinned=True,
        tenant_id="tenant-a",
    ),
)
```

The `producer_principal_id` stamped on the event at the request
boundary (ADR-066 §4.1) is the input to `acl.check`. Events that
predate v0.16 (`producer_principal_id=None`) are denied when
`acl` is set — the audit trail records `acl_denied_no_principal`.

### 0.2 Gate 2 — persona of the agent (business system)

The agent's `RoleComponent` (projected from `role.swapped`) carries
`allowed_tools`, the explicit allow-list of **systems** the persona
may use. The business system checks
`has_tool_access(role, system_name)` **before** emitting
`tool.<name>.requested`. When the persona forbids the system, the
system emits `intent.validation_failed` (or `tool.<name>.failed`
with `error="access_denied"`) instead — the tool call is never
dispatched.

```python
from kntgraph.core.components.role import RoleComponent, has_tool_access

role = view.components.get(RoleComponent)
if not has_tool_access(role, "chat"):
    # gate 2 blocks at the source; no tool.requested is emitted
    ...
```

### 0.3 The tool is accessed only through its owning system

The two gates are wired together by the rule that **a tool is
accessed only through its owning business system**. The caller
(the intent) talks to the system (e.g. `"chat"`), never to the
tool directly. Gate 2 validates the **system** name against the
persona; gate 1 validates the **principal** against the tool's
ACL. Because the tool is internal to the system, renaming or
swapping the underlying tool does not change the caller's
contract.

### 0.4 Worked example

See [`examples/21_two_level_authorization.py`](../../examples/21_two_level_authorization.py)
for a runnable three-scenario walkthrough:

1. **Allow** — persona admits `"chat"` + principal in `tenant-a`:
   both gates pass, the tool runs.
2. **Gate 2 blocks** — persona forbids `"chat"`: the system emits
   `tool.chat_llm.failed` with `error="access_denied"`; no
   `tool.requested` is emitted.
3. **Gate 1 blocks** — persona admits `"chat"` but the principal
   is in `tenant-b`: the request is emitted but the `WorkerManager`
   denies it against the tenant-pinned ACL.

```bash
KNT_REDIS_FAKE=1 uv run python examples/21_two_level_authorization.py
```

---

## 1. What you get

After enabling L2:

- A `CapabilityPolicy` per `agent_id` declaring which
  `event_type`s that agent may emit (allow-list + deny-list).
- `EventLog.append` checks the policy **after** signature
  verification, **before** Stream append.
- Rejected events raise `EventTypeForbidden` (default) or are
  logged and skipped (when `policy_warn_only=True`).
- Per-agent rate limit (`max_event_rate_per_sec`) via Redis
  sliding window.
- `KeyRegistry.revoke(agent_id, key_epoch, reason)` invalidates
  a compromised key without losing verification history for
  events signed before the revocation.

---

## 2. Concepts

### 2.1 `CapabilityPolicy`

```python
# src/kntgraph/security/authorization.py
@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """Per-agent_id authorisation for event_type emission.

    Evaluated by EventLog.append AFTER signature verifies
    (L1) and BEFORE Stream append.
    """
    agent_id: str
    allowed_event_types: frozenset[str]            # wildcard: {"*"}
    denied_event_types: frozenset[str] = frozenset()
    max_event_rate_per_sec: Optional[int] = None
    require_signature: bool = True
```

Semantics:

- If `event.event_type ∈ denied_event_types` → **deny** (even
  if in `allowed_event_types`).
- If `allowed_event_types == {"*"}` → all types allowed except
  those in `denied_event_types`.
- If `require_signature=True` and `event.signature is None` →
  deny.
- If `max_event_rate_per_sec` is exceeded → deny.

### 2.2 `PolicyRegistry` Protocol

```python
class PolicyRegistry(Protocol):
    def get(self, agent_id: str) -> CapabilityPolicy: ...
    def set(self, policy: CapabilityPolicy) -> None: ...
    def revoke(self, agent_id: str, key_epoch: int, reason: str) -> None: ...
```

v1 ships `InMemoryPolicyRegistry`. v2 plugs in a
`RedisPolicyRegistry` (durable across process restarts) and a
`VaultPolicyRegistry` (config-as-code with audit trail).

### 2.3 Rate limit (sliding window)

`max_event_rate_per_sec` is implemented as a Redis sorted set
with timestamps:

```
ZADD knt:rate:{agent_id} <unix_ms> <event_id>
ZREMRANGEBYSCORE knt:rate:{agent_id} -inf <unix_ms - 1000>
ZCARD knt:rate:{agent_id}  # current rate
```

The check is atomic via a Lua script (provided in
`kntgraph.security.rate_limit`). Failure mode: if Redis is
down, **fail closed** (reject) — never fail open in a
zero-trust deployment.

### 2.4 Revocation

`KeyRegistry.revoke(agent_id, key_epoch, reason)` adds an entry
to:

- `KeyRegistry._revoked: dict[(agent_id, key_epoch), str]` —
  in-memory.
- `knt:revocations:{agent_id}` — Redis sorted set, for cross-
  verifier propagation.

`verify_event` checks `is_revoked(agent_id, signature.key_epoch)`
**before** verifying the cryptographic signature. A revoked key
fails verify (returns `False`) without raising.

Revocation does **not** invalidate events signed under the
revoked key before revocation time. Those events continue to
verify (auditor can replay them). Only **future** events signed
under the revoked key are rejected.

---

## 3. Wiring through `EventLog`

### 3.1 Configuration

```python
from kntgraph.security.authorization import (
    InMemoryPolicyRegistry,
    CapabilityPolicy,
)

policy_registry = InMemoryPolicyRegistry()
policy_registry.set(CapabilityPolicy(
    agent_id="session-42",
    allowed_event_types=frozenset({"pedido.received", "estoque.check"}),
    denied_event_types=frozenset({"process.cancelled"}),
    max_event_rate_per_sec=10,
    require_signature=True,
))

log = EventLog(
    redis,
    key_registry=key_registry,        # L1
    policy_registry=policy_registry,    # L2 (this)
    require_signatures=True,
)
```

### 3.2 YAML configuration (kinetgraph)

```yaml
# examples/pedido.yml
process:
  id: pedido-v1
  steps:
    - id: receber
      role: Atendente
      action: receive_pedido

agents:
  session-42:
    allowed_event_types:
      - pedido.received
      - estoque.check
    denied_event_types:
      - process.cancelled
    max_event_rate_per_sec: 10
    require_signature: true

  office-engine:
    allowed_event_types: ["*"]
    max_event_rate_per_sec: 1000
    require_signature: true
```

`PedidoRunner` parses the `agents:` block and configures
`PolicyRegistry` at boot.

### 3.3 Append path (L1 + L2)

```
EventLog.append(event)
    │
    ├─► signature verify         (L1)
    │     └─► SignatureVerificationFailed  → reject
    │
    ├─► policy.get(event.agent_id)
    │     ├─► policy.denied check
    │     ├─► policy.allowed check
    │     ├─► policy.rate_limit check
    │     └─► any fail → EventTypeForbidden  → reject
    │
    └─► XADD knt:agents:{agent_id}:events
```

Order matters: signature first (cheap, cryptographic), policy
second (may hit Redis for rate limit). Reversing the order
allows an attacker to DoS the rate limit check by sending
unsigned events.

---

## 4. Rate limit: details

### 4.1 Sliding window algorithm

```lua
-- atomic check + record
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local event_id = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window_ms)
local count = redis.call('ZCARD', key)
if count >= limit then
    return 0  -- rejected
end
redis.call('ZADD', key, now, event_id)
redis.call('PEXPIRE', key, window_ms * 2)
return 1  -- accepted
```

### 4.2 Failure modes

| Scenario | Behaviour |
|---|---|
| Redis OK, under limit | accept |
| Redis OK, over limit | reject (`RateLimitExceeded`) |
| Redis down | **fail closed**: reject with `RateLimitBackendUnavailable` |
| Lua script error | reject with `RateLimitScriptError` (logged + alert) |

Fail-closed is the zero-trust default. A `rate_limit_fail_open=True`
override exists for non-critical pipelines (debug, dev).

### 4.3 Per-event-type rate limit (v2)

`max_event_rate_per_sec` is per-agent today. v2 adds
`max_event_rate_per_sec_per_type: dict[str, int]` for finer
control (e.g. allow 1000 `pedido.received`/s but only 10
`process.cancelled`/s).

---

## 5. Revocation: details

### 5.1 Lifecycle

```
0.  register(agent_id, priv)            # key_epoch = 0
1.  sign events under epoch=0           # verified by epoch=0 verifier
2.  revoke(agent_id, key_epoch=0, ...)  # epoch=0 revoked
3.  register(agent_id, new_priv)        # key_epoch = 1
4.  sign new events under epoch=1       # verified by epoch=1 verifier
5.  old events (signed epoch=0)         # still verify (auditor)
6.  new events forged with epoch=0 key  # rejected (epoch=0 revoked)
```

### 5.2 Audit trail

Revocations are appended to `knt:revocations:{agent_id}` as a
Redis Stream (immutable, append-only):

```
XADD knt:revocations:session-42
    * key_epoch 0
      reason "operator_key_compromised_2026_06_22"
      revoked_by "operator-id-42"
      revoked_at 2026-06-22T10:00:00Z
```

Auditors can replay the revocation history via
`XREAD knt:revocations:session-42`.

### 5.3 Propagation

The revocation is checked **locally** by every verifier
(in-memory `_revoked` map, hydrated at boot from the Redis
Stream). For multi-region deployments, v2 ships a gossip
protocol with eventual consistency; v1 ships single-region
with the explicit warning *"revocation propagation within
cache TTL + 60s"*.

---

## 6. Worked example

```python
import asyncio
import redis.asyncio as aioredis
from kntgraph.stream.event_log import EventLog
from kntgraph.security.keys import InMemoryKeyRegistry, generate_keypair
from kntgraph.security.signing import sign_event
from kntgraph.security.authorization import (
    InMemoryPolicyRegistry,
    CapabilityPolicy,
)

async def main():
    redis = aioredis.from_url("redis://localhost:6379")

    key_registry = InMemoryKeyRegistry()
    priv, _ = generate_keypair()
    key_registry.register("session-42", priv=priv)

    policy_registry = InMemoryPolicyRegistry()
    policy_registry.set(CapabilityPolicy(
        agent_id="session-42",
        allowed_event_types=frozenset({"pedido.received"}),
        max_event_rate_per_sec=10,
    ))

    log = EventLog(
        redis,
        key_registry=key_registry,
        policy_registry=policy_registry,
        require_signatures=True,
    )

    # 1. Allowed event
    e1 = build_event("pedido.received", "session-42")
    await log.append(sign_event(e1, key_registry.private_key("session-42")))
    print("OK: pedido.received allowed")

    # 2. Forbidden event_type
    e2 = build_event("process.cancelled", "session-42")
    try:
        await log.append(sign_event(e2, key_registry.private_key("session-42")))
    except Exception as exc:
        print(f"REJECTED: {type(exc).__name__}: {exc}")
        # REJECTED: EventTypeForbidden: agent_id=session-42 cannot emit
        # event_type=process.cancelled (not in allowed set)

    # 3. Revoke the key
    key_registry.revoke("session-42", key_epoch=0, reason="test")
    e3 = build_event("pedido.received", "session-42")
    try:
        await log.append(sign_event(e3, key_registry.private_key("session-42")))
    except Exception as exc:
        print(f"REJECTED: {type(exc).__name__}: {exc}")
        # REJECTED: SignatureVerificationFailed: key_epoch=0 revoked

    await redis.aclose()

asyncio.run(main())
```

---

## 7. Policy debugging

### 7.1 Dry-run mode

```python
policy_registry = InMemoryPolicyRegistry()
policy_registry.set(...)

log = EventLog(redis, policy_registry=policy_registry, policy_dry_run=True)

# Log shows what WOULD be rejected without actually rejecting.
await log.append(event)
# logs: {"would_reject": true, "reason": "event_type_forbidden", ...}
```

Useful for migration windows: deploy with `policy_dry_run=True`,
collect metrics on false positives, then switch to enforcement.

### 7.2 Audit query

```python
# How many rejections in the last 24h?
count = await policy_registry.audit_log.count(
    agent_id="session-42",
    since=now - timedelta(hours=24),
)
```

The audit log is `knt:policy_audit` (Redis Stream); each
rejection is one entry with reason, agent_id, event_type,
timestamp.

---

## 8. Performance

| Operation | Median | p99 |
|---|---|---|
| `policy.get(agent_id)` | 200ns | 1µs |
| `rate_limit.check(...)` (Lua) | 120µs | 280µs |
| **`EventLog.append` overhead vs L0** | **+150µs** | **+330µs** |

L2 adds ~150µs/event vs L1 — driven by the rate-limit Lua call.
For the kinetgraph MVP, total overhead vs L0 is ~480µs/event
(sign + verify + policy + rate-limit). Still negligible
against the 1.5s smoke-test budget.

---

## 9. Common pitfalls

### 9.1 Confusing `allowed_event_types={"*"}` with no policy
`*` is a wildcard that means "all except denied". An empty
frozenset `frozenset()` means "no types allowed". A missing
policy (registry returns default-deny) means **nothing
allowed**. Be explicit; never rely on missing policies.

### 9.2 Forgetting to refresh policies after deploy
`InMemoryPolicyRegistry` is in-process. A new process boots
with the policies in code; restart to pick up new YAML. v2
ships `RedisPolicyRegistry` for hot-reload.

### 9.3 Setting `max_event_rate_per_sec` too low for legitimate bursts
The kinetgraph engine emits ~5 events/pedido in a burst. A
rate limit of 1/s will reject legitimate traffic. Set the
limit at **2-3× peak observed rate** and use `policy_dry_run`
to validate before enforcement.

### 9.4 Forgetting revocation propagation
The verifier's `_revoked` map is hydrated at boot. A new
revocation is not seen by other verifiers until they re-hydrate
(default: every 60s) or receive a gossip message (v2). For
high-stakes revocations, force `flush_revocation_cache()` on
all verifiers.

### 9.5 Failing open on rate-limit backend failure
The default is **fail closed**. Setting
`rate_limit_fail_open=True` in non-debug environments
defeats the purpose of L2. Document and review.

---

## 10. Testing your integration

```python
def test_policy_rejects_unauthorized_event_type():
    """EventTypeForbidden raised when event_type not in allowed."""

def test_policy_allows_wildcard_with_deny():
    """allowed={'*'}, denied={'process.cancelled'} → others OK."""

def test_rate_limit_rejects_overflow():
    """11th event in 1s rejected with RateLimitExceeded."""

def test_revoked_key_rejected():
    """After revoke, sign_event produces a sig that fails verify."""

def test_old_events_still_verify_after_revoke():
    """Pre-revocation signed events continue to verify."""

def test_signature_required_when_policy_says_so():
    """policy.require_signature=True + event.signature=None → reject."""
```

---

## 11. Migration from L1

1. **Declare policies in YAML.** Add `agents:` block to your
   `kinetgraph` config or `CapabilityPolicy` set calls.
2. **Deploy with `policy_dry_run=True`.** Collect metrics on
   what would be rejected.
3. **Switch to `policy_dry_run=False`.** Rejections now
   surface as `EventTypeForbidden` (or warn-only).
4. **Enable rate limit per agent.** Start generous
   (3× peak); tighten over weeks.
5. **Document the revocation runbook.** Who can revoke? How
   is it propagated? What's the SLA?

---

## 12. See also

- [signing.md](./signing.md) — Level 1 (authentication)
- [anchor.md](./anchor.md) — Level 3 (tamper evidence)
- [threat_model.md](./threat_model.md) — formal threat model
- [README.md](./README.md) — overview of all levels
- [ADR-016](../../ADRs/ADR-016-Event-Signing.md) — L1 design
- ADR-017a (proposed) — L2 design
- [ADR-060 §3.0](../../ADRs/ADR-060-fmh-office-v2-pillars.md) —
  the three-gate ACL model (gate 1 + gate 2 + handoff)
- [ADR-066 §4.1](../../ADRs/ADR-066-Single-Tool-Path.md) — the
  single tool path and the `WorkerManager` ACL hook
- [`examples/21_two_level_authorization.py`](../../examples/21_two_level_authorization.py)
  — runnable two-gate walkthrough
