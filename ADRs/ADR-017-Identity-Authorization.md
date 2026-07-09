<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-017: Identity Propagation and Authorization Model (Zero-Trust Level 2)

| | |
|---|---|
| **Status** | Proposed |
| **Date** | 2026-06-23 |
| **Deciders** | @adriano |
| **Supersedes** | — |
| **Related** | ADR-001 (Architecture), ADR-005 (Idempotency), ADR-012 (IntentRouter), ADR-015 (fmh_office), ADR-016 (Event Signing), AUDIT HIGH #11 (RBAC + tenant scoping) |

> **Note to reviewers:** This ADR fixes the **identity model** (Tiers 1, 2, 3) and the **migration strategy** for it. The **tool-level ACL design** (§5) is intentionally presented as a set of open scenarios for feedback before we lock the contract. Please reply with which scenario matches your intended use, or propose a fifth.

---

## 1. Context

The FMH framework today answers the question *"who is calling?"* via the `X-API-Key → agent_id` mapping in `RedisAPIKeyVerifier` (ADR-012). It does **not** answer the question *"what are they allowed to do?"*. The current state:

- **No roles.** Any authenticated agent can call any endpoint and any registered tool.
- **No tenant boundary.** An agent in `tenant-A` can append events with `agent_id = "tenant-B.some-agent"`. The `EventLog` accepts it (the in-process `agent_id` is not validated against the authenticated principal at the boundary; **B2** closed the character-set attack but not the cross-tenant attack).
- **No tool-level ACL.** `ToolRegistry` indexes tools by name; any authenticated agent can `invoke` any registered tool.
- **No audit of denials.** A 403 today (auth-failed) is logged but a missing 403 (auth-ok-but-shouldn't) is invisible.

Two concrete scenarios motivate this ADR:

1. **Multi-tenant SaaS**. An operator hosts multiple customer tenants on a single FMH deployment. Tenant A's API key, if leaked, must not let an attacker read tenant B's events, invoke tenant B's tools, or impersonate tenant B's agents.

2. **Service-accounts with escalation risk**. A background job (e.g. a knowledge consolidator) holds a service-account key. Today it can call `tool.admin.rebuild_index` because no role check exists. We want to forbid this at the framework boundary, not by convention.

This ADR is **Zero-Trust Level 2** in the same ladder ADR-016 introduced for event signing:

| Level | Mechanism | Status |
|---|---|---|
| L0 | API key authentication | shipped |
| L1 | Ed25519 event signatures | shipped (ADR-016) |
| **L2** | **Principal + role + tenant + tool ACL** | **this ADR** |
| L3 | Per-event authorization (e.g. ABAC, time-bound tokens) | future |

---

## 2. Decision: Identity Model (FIXED)

### 2.1 Three-tier model

**Tier 1 — Principal (the answer to "who").** A `Principal` is an immutable record carried through the request and event lifecycle:

```python
@dataclass(frozen=True, slots=True)
class Principal:
    agent_id: str           # the producer identity (existing)
    role: Role              # admin | agent | service
    tenant_id: str          # tenant scope
    key_id: str             # the API key identifier (for revocation)

class Role(StrEnum):
    admin = "admin"         # cross-tenant operations
    agent = "agent"         # the producer default
    service = "service"     # background workers / consolidators
```

**Tier 2 — Authentication (proving identity).** `X-API-Key → Principal`. The `RedisAPIKeyVerifier` today returns `agent_id: str`. Under Zero-Trust, it returns `Principal`. The Redis binding table is extended:

```
# Legacy binding (kept for compat):
knt:api:keys:<sha256(key)>  →  agent_id

# Zero-Trust binding:
knt:api:keys:<sha256(key)>  →  JSON {
    "agent_id":  "agent-A1",
    "role":      "agent",
    "tenant_id": "tenant-A",
    "key_id":    "k-2026-06-23-001"
}
```

**Tier 3 — Authorization (deciding "may they?").** A `Policy` evaluates a `(Principal, Resource, Action)` triple. Resources are typed; actions are an enum.

```python
class Policy(Protocol):
    def allows(
        self,
        *,
        principal: Principal,
        resource: Resource,
        action: Action,
    ) -> bool: ...
```

Three enforcement points (fixed in this ADR):

| Point | Where | What it checks |
|---|---|---|
| **Request** | FastAPI `Depends` | role + tenant at the route boundary |
| **Route** | Path prefix | `/admin/*` requires `role=admin` |
| **Event emission** | `EventLog.append` (already validated in B2) | `event.agent_id.tenant_id` matches `principal.tenant_id` |

The fourth point — **Tool ACL** — is §5 (open scenarios).

### 2.2 Tenant derivation (FIXED — DECISION #1)

**`tenant_id` lives in the API-key binding, not in any HTTP header.**

Rationale: an HTTP header (`X-Tenant`) is spoofable by anyone with the API key — the same secret that authenticates the request can lie about the tenant. The binding table is the source of truth; the verifier resolves it server-side and returns a `Principal` whose `tenant_id` cannot be tampered with from outside.

Consequence: a tenant is "the set of principals that share a `tenant_id`". A `Principal` with `role=admin` may have `tenant_id=None` (cross-tenant admin); an `agent` or `service` must have a non-null `tenant_id`. The `agent_id` of an agent principal **must start with** its `tenant_id` (e.g. `tenant-A.agent-1`); this is a soft convention enforced by the binding, not by the framework's regex.

### 2.3 Roles (FIXED — DECISION #2)

Three initial roles, listed with their canonical capabilities:

| Role | Tenant scope | Can read own tenant | Can read other tenants | Can invoke admin tools | Typical caller |
|---|---|---|---|---|---|
| `admin` | `None` (cross-tenant) | yes | yes | yes | operator / control-plane |
| `agent` | required, non-null | yes | no | no | user-facing agent |
| `service` | required, non-null | yes | no | no (and explicitly forbidden to call any tool whose `required_role != agent`) | consolidator, projector, scheduler |

The `admin` role is the only one that crosses tenant boundaries. The `service` role is **distinct** from `agent` because services need a stronger guarantee: their behaviour is bounded by the framework, not by the developer's convention.

### 2.4 Migration strategy (FIXED — DECISION #3)

We ship Zero-Trust as a **feature-flagged mode** to avoid breaking every consumer at once:

```
KNT_AUTH_MODE=legacy      # default in 0.8.x — current behaviour
KNT_AUTH_MODE=zero_trust  # opt-in; becomes default in 0.9.0
```

In `legacy` mode:
- The verifier still returns `agent_id: str` (a legacy `Principal` with `role="agent"`, `tenant_id="default"`, `key_id="legacy"`).
- The `Policy` is a no-op (`allows(...) == True` for everything).
- All existing tests and deployments keep working.

In `zero_trust` mode:
- The verifier returns the full `Principal`.
- The `Policy` enforces request/route/event-tool checks (per §2.1).
- The `EventLog.append` rejects events whose `agent_id` is not under the principal's `tenant_id` (and `agent_id` does not start with `principal.tenant_id + "."`).

A deployment flips by setting `KNT_AUTH_MODE=zero_trust` and updating the binding table. The mode is a single source-of-truth read by `fresh_settings()`; flipping does not require a code change.

**Removal timeline**: `legacy` mode is removed in `0.10.0` (one minor version after `zero_trust` becomes default).

---

## 3. Decision: `Principal` lifecycle

### 3.1 Authentication: how a `Principal` enters the request

`X-API-Key` → `APIKeyVerifier.verify(key)` → `Result[Principal, AuthError]`. Three failure modes map to HTTP status:

| Failure | HTTP | Audit log |
|---|---|---|
| `missing` | 401 | `auth.missing_key` |
| `forbidden` (key not recognised) | 403 | `auth.unknown_key` (with sha256(key) prefix, never the raw key) |
| `tenant_mismatch` (key changed tenants, or `tenant_id` violates §2.2) | 403 | `auth.tenant_violation` |

The `Principal` is then bound to the request via `Depends(get_principal)` (replaces the current `_auth`/`require_agent` depending). It flows into the handler signature as a typed argument; `mypy` will catch any handler that forgets to type it.

### 3.2 Authorisation at the request boundary

A route declares its required policy via a decorator (or `Depends`):

```python
@app.post("/admin/rebuild-index")
async def rebuild_index(
    principal: Principal = Depends(require_role(Role.admin)),
): ...
```

The framework provides three pre-built dependencies:

| Dependency | Effect |
|---|---|
| `require_role(Role.admin)` | 403 if `principal.role != admin` |
| `require_role(Role.admin, Role.service)` | 403 if role not in set |
| `require_tenant(principal_tenant)` | 403 if `principal.tenant_id != principal_tenant` |

Custom policies are written as `Policy` implementations and composed with `Depends(policy_factory(p))`.

### 3.3 Propagation through `EventLog.append`

The `Principal` is **not** stored on every event (that would inflate every event with a metadata blob). Instead, the principal is bound to the **request coroutine** via a `ContextVar` populated by the FastAPI middleware at the request boundary. The `EventLog.append` reads it before XADD:

```python
# In the middleware:
principal_ctx.set(principal)

# In EventLog.append (already B2-validated for agent_id):
principal = principal_ctx.get()
if auth_mode == "zero_trust":
    if principal.role != Role.admin:
        if not event.agent_id.startswith(principal.tenant_id + "."):
            return Err(PersistenceError("tenant_violation"))
```

`ContextVar` is the right tool here: it's per-task, propagates through `await`, and is automatically scoped to the request when FastAPI's middleware spawns per-request tasks.

---

## 4. Decision: routing rules (FIXED)

Routes partition into three prefixes:

| Prefix | Required role | Required tenant |
|---|---|---|
| `/healthz`, `/readyz`, `/docs`, `/redoc`, `/openapi.json` | none | none (bypass) |
| `/admin/*` | `Role.admin` | none (cross-tenant OK) |
| `/agents/{agent_id}/*` | any | `principal.tenant_id` matches the segment `{agent_id}`'s tenant (or `principal.role == admin`) |
| `/intents`, `/status` | `Role.agent` or `Role.admin` or `Role.service` | `principal.tenant_id` matches the body/header `agent_id` |

The `/admin/*` namespace is new in this ADR; it is empty in `0.8.x` and grows as admin endpoints are added.

---

## 5. Tool-level ACL: open scenarios (DECISION #4 — PLEASE REVIEW)

The three enforcement points in §2.1 cover request/route/event-emission. The fourth — **which tools a principal may invoke** — is intentionally left open pending your feedback. Four candidate designs follow; the team should pick one before code lands.

### Scenario A — Coarse role check (`required_role`)

```python
class ToolDescriptor:
    name: str
    required_role: Role = Role.agent

# ToolInvoker:
if principal.role < tool.required_role:  # via Role ordering
    raise ToolError("role_insufficient")
```

Pros: simple, 5 lines of code, no schema migration beyond one field.
Cons: an `agent` in `tenant-A` can invoke any agent-scoped tool in `tenant-A`. There is no way to say *"only agents X, Y, Z may call `tools.billing.refund`"*.

### Scenario B — Tenant + role (`required_role` + `tenant_pinned`)

```python
class ToolDescriptor:
    name: str
    required_role: Role = Role.agent
    tenant_pinned: bool = False  # when True, only the owning tenant

# ToolInvoker:
if principal.role < tool.required_role:
    raise ToolError("role_insufficient")
if tool.tenant_pinned and principal.role != Role.admin:
    if tool.tenant_id != principal.tenant_id:
        raise ToolError("tenant_violation")
```

Pros: catches the cross-tenant `agent` case.
Cons: still no per-agent ACL; `agent-X` and `agent-Y` in the same tenant are interchangeable.

### Scenario C — Policy object (Python predicate)

```python
@dataclass(frozen=True)
class ToolPolicy:
    tool_name: str
    allow: Callable[[Principal], bool]  # evaluated per call

# ToolRegistry:
registry.register(tool, policy=ToolPolicy(
    tool_name="tools.billing.refund",
    allow=lambda p: p.role == Role.admin or
                   p.agent_id in {"agent-finance-X", "agent-finance-Y"},
))

# ToolInvoker:
if tool.policy is not None and not tool.policy.allow(principal):
    raise ToolError("policy_denied")
```

Pros: maximal flexibility; supports any predicate.
Cons: predicate is per-call (slow for high-throughput tools); no static check of "who can call what"; the predicate itself can have bugs (and `allow` runs on every invocation).

### Scenario D — Static ACL table (declarative)

```python
# Configured in code or via the API-key admin endpoint:
ACL = {
    "tools.billing.refund": frozenset({Role.admin, Role.service}),
    "tools.knowledge.upsert": frozenset({Role.service, Role.admin}),
    "tools.echo": frozenset({Role.agent, Role.service, Role.admin}),
}

# ToolInvoker:
if principal.role not in ACL.get(tool.name, {Role.admin}):
    raise ToolError("role_insufficient")
```

Pros: auditable (table is a dict literal, easy to read); static (no Python callbacks); can be hot-reloaded from a config file.
Cons: still role-only — no per-agent ACL; coarse-grained sharing ("all services").

### Comparison

| | A | B | C | D |
|---|---|---|---|---|
| Lines of code | ~5 | ~10 | ~20 | ~15 |
| Cross-tenant guard | ❌ | ✅ | ✅ | ❌ |
| Per-agent ACL | ❌ | ❌ | ✅ | ❌ |
| Static / auditable | ✅ | ✅ | ❌ | ✅ |
| Per-call cost | O(1) | O(1) | O(predicate) | O(1) |
| Schema migration | 1 field | 1 field | 1 field + policy registry | 1 module-level dict |

**Recommendation (open for review)**: **Scenario B**. Cross-tenant guard is the concrete multi-tenant risk; the per-agent ACL of C can be added later (backward-compatible — adding a `policy` field to `ToolDescriptor` is additive).

---

## 6. Consequences

### Positive

- **Tenant isolation becomes structural**, not by convention.
- **Service-account scope is bounded**: a service-account key cannot accidentally invoke an admin tool (because the framework refuses).
- **Authorization is auditable**: every denial (`auth.unknown_key`, `auth.role_insufficient`, `auth.tenant_violation`) is logged with the sha256 key prefix (never the raw key) and the principal's role/tenant.
- **Migration is staged**: `legacy` mode keeps existing deployments running; `zero_trust` is opt-in.

### Negative

- **API breaking change for verifier consumers** (in `zero_trust` mode): `Result[str, AuthError]` becomes `Result[Principal, AuthError]`. Mitigated by the `legacy` mode flag and a one-version deprecation window.
- **Tool ACL is coarser than the original audit asked for** (per-agent is not in the chosen scenario). If a tenant later needs per-agent ACL, we add Scenario C as a backward-compatible extension.
- **ContextVar propagation depends on `asyncio` task structure**. Long-running background tasks (consolidator loops) need to either (a) bind their own principal at task start, or (b) be wrapped by a `with_principal(...)` context manager. This is documented but easy to forget; an integration test pins it.

### Risks

- **`legacy` mode never turned off**: if a deployment never flips `KNT_AUTH_MODE`, they stay at L0 forever. Mitigated by the deprecation timeline (`0.10.0` removes `legacy`); operators see the warning in `Settings` validator output.
- **Predicate in `Scenario C` is a footgun**: not chosen by §5 recommendation, but if added later, must be sandboxed (no `eval`, no filesystem access, no network).
- **`ContextVar` leak across awaits**: mitigated by per-request middleware; the `EventLog.append` reads but never writes the principal.

---

## 7. Migration path (FIXED — DECISION #3 in detail)

### 7.1 Phase 1 (`0.8.x`) — feature-flag shipped, default `legacy`

1. Add `Principal`, `Role`, `Policy` types — additive.
2. Add `Settings.auth_mode: Literal["legacy", "zero_trust"]`.
3. Extend `RedisAPIKeyVerifier` to return `Principal` **only when** `auth_mode == "zero_trust"`; otherwise return a legacy `Principal(agent_id, agent, "default", "legacy")`.
4. The `Policy` and middleware are **no-ops in `legacy` mode**.
5. Tests in `tests/unit/api/test_principal.py` cover both modes.

### 7.2 Phase 2 (`0.9.0`) — default flips to `zero_trust`

1. `Settings.auth_mode` default becomes `"zero_trust"`.
2. `legacy` mode still works (with a `DeprecationWarning` on import).
3. Migration docs (`NEXT_STEPS.md` Path D) walk operators through updating the binding table.

### 7.3 Phase 3 (`0.10.0`) — `legacy` removed

1. `legacy` is deleted.
2. `Settings.auth_mode` is removed; the framework is Zero-Trust Level 2 by default.

---

## 8. References

- AUDIT HIGH #11 (RBAC + tenant scoping) — original finding
- ADR-005 (Idempotency) — request → event correlation
- ADR-012 (IntentRouter) — current HTTP auth contract
- ADR-015 (fmh_office vertical) — current single-tenant pattern
- ADR-016 (Event Signing) — Zero-Trust Level 1; this ADR is Level 2
- **B2** (agent_id trust boundary) — character-set validation; this ADR layers tenant semantics on top
- **B3** (Idempotency-Key trust boundary) — orthogonal; not affected by this ADR
- **B5** (HTTP rate limit) — per-IP today; per-tenant follow-up after this ADR lands
- **B6** (pip-audit) — orthogonal

---

## 9. Open question for reviewers

**§5 Tool-level ACL** — please pick a scenario (A / B / C / D) or propose a fifth. Once locked, the contract is:

```python
@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]
    required_role: Role
    tenant_pinned: bool            # only with Scenario B/C/D
    policy: ToolPolicy | None       # only with Scenario C
```

and `ToolInvoker` enforces it. Without a decision, §5 is the last unblocked item on the path to L2.