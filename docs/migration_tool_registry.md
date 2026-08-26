<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Migration guide — `ToolRegistry` → `WorkerManager` (v0.16)

ADR-066 closes the gap that ADR-061 §5 flagged: the
``chat_llm`` tool (and every other tool) was an
"unauthenticated tool" — the dispatcher emitted the
request, but no gate-1 ACL check ran before the
worker. The fix moves ACL enforcement to the
canonical home (``WorkerManager``), adds a new field
to ``Event`` for principal provenance, and ships the
migration path in three minors (v0.16 / v0.17 /
v0.18).

This guide covers **v0.16** — the slice that lands in
this release: the ACL hook on ``WorkerManager``,
``WorkerManager.acl_for(name)``, and the
``Event.producer_principal_id`` schema change. v0.17
and v0.18 are covered in a follow-up.

---

## 1. `WorkerManager.register(tool_cls, *, acl=...)`

### Before (pre-v0.16)

```python
# tools/registry.py — the legacy ACL home
from kntgraph.tools import ToolRegistry, default_acl

registry = ToolRegistry()
registry.register(my_tool)  # no ACL hook
```

### After (v0.16)

```python
# WorkerManager is the canonical home (ADR-066 §3.1).
from kntgraph.tools import WorkerManager, default_acl

worker_manager = WorkerManager(redis=..., event_log=...)
worker_manager.register(my_tool, acl=default_acl())
```

### ACL semantics

| `register(...)` call | Behaviour |
|---|---|
| `register(tool_cls)` (no `acl=`) | **No constraint** — legacy contract, every request is invoked. |
| `register(tool_cls, acl=default_acl())` | Baseline: every request is checked against `Role.agent`. |
| `register(tool_cls, acl=ToolACL(...))` | Custom policy: the worker's gate-1 check rejects on `acl.check(p) is False`. |

The v0.17 step (ADR-066) flips the default to
``default_acl()`` — calls without ``acl=`` will emit a
``DeprecationWarning`` and behave as if ``acl=`` was
passed. v0.18 removes the legacy path entirely.

---

## 2. `WorkerManager.acl_for(name)` accessor

### Before

```python
acl = registry.acl_for("my_tool")
if acl is not None:
    ok, reason = acl.check(principal)
    if not ok:
        ...
```

### After

```python
acl = worker_manager.acl_for("my_tool")  # returns None for legacy
if acl is not None:
    ok, reason = acl.check(principal)
    if not ok:
        ...
```

The surface is identical — one-line rename from
``registry.acl_for(n)`` → ``worker_manager.acl_for(n)``.

---

## 3. `Event.producer_principal_id`

### What changed

``Event`` gained an optional ``producer_principal_id:
str | None`` field (ADR-066 §4.1). The dispatcher sets
it from ``principal_ctx`` at the request boundary; the
worker's gate-1 check reads it for the ACL lookup.

### Wire format

The new field is encoded in the wire format as
``"producer_principal_id": "..."`` (or empty string
when absent). ``from_dict`` decodes empty string to
``None`` (the pre-v0.16 contract). Old consumers that
ignore unknown keys continue to work.

### Forward compatibility

Events written before v0.16 do not have the field.
When the worker sees ``producer_principal_id is None``
and ``acl`` is set, it denies with
``acl_denied_no_principal``. The audit trail records
the reason so the operator can diagnose.

---

## 4. Behaviour changes summary

| Scenario | Pre-v0.16 | v0.16 (this slice) |
|---|---|---|
| Tool registered without `acl=` | Invoked | Invoked (legacy contract) |
| Tool registered with `acl=default_acl()`, principal at `Role.agent` | **Not enforced** (unauthenticated) | Invoked |
| Tool registered with `acl=`, principal below `agent` | **Not enforced** (unauthenticated) | Denied with `acl_denied:role_insufficient` |
| Tool registered with `acl=`, event without `producer_principal_id` | **Not enforced** (unauthenticated) | Denied with `acl_denied_no_principal` |
| Tool registered with `acl=`, principal at `Role.admin` | **Not enforced** | Invoked |

The "not enforced" rows are the gap that ADR-061 §5
flagged. v0.16 closes the gap; v0.17 flips the default
to ``default_acl()`` so the legacy path emits a warning;
v0.18 removes the legacy path entirely.

---

## 5. Migration checklist

For each ``ToolRegistry.register(...)`` call site:

  1. Replace ``ToolRegistry()`` with
     ``WorkerManager(...)`` (the WorkerManager holds the
     tool registry and the executor pool together).
  2. Pass ``acl=`` on each ``register(...)``. The
     canonical baseline is ``acl=default_acl()`` for
     tenant-unscoped tools; use
     ``acl=ToolACL(tenant_pinned=True, tenant_id=...)``
     for tenant-scoped tools.
  3. Replace ``registry.acl_for(name)`` with
     ``worker_manager.acl_for(name)``.
  4. Stamp ``producer_principal_id`` on each event
     emitted at the request boundary (the API layer's
     ``POST /intents`` does this in v0.16+; pre-v0.16
     events pass ``None`` and the worker denies with
     ``acl_denied_no_principal``).
  5. Drop the import of ``ToolRegistry``.

The CLI scaffolds (``cli/templates/dispatcher.py.jinja``
et al.) are updated in v0.17 to register via
``WorkerManager.register(acl=default_acl())`` by
default. Examples migrate in the same release.

---

## 6. See also

- [ADR-066](../ADRs/ADR-066-Single-Tool-Path.md) — the
  full decision record (3-minor migration plan,
  acceptance checklist, alternatives).
- [ADR-061 §5](../ADRs/ADR-061-litellm-integration-review.md)
  — the original gap: "``chat_llm`` is an
  unauthenticated tool."
- [ADR-060 §3.0](../ADRs/ADR-060-fmh-office-v2-pillars.md)
  — the three-gate ACL model.
- [DEBT §2.27](../DEBT.md) — the predecessor work
  record (v0.14 entry that closed the same gap in
  isolation but couldn't fix the WorkerManager path).
