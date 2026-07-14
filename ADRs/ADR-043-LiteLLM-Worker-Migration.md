# ADR-043: LiteLLM worker migration + ToolInvoker deprecation

**Status:** Proposed
**Date:** July 14, 2026
**Related to:** [ADR-007](./ADR-007-LiteLLM-Adapter.md), [ADR-036](./ADR-036-Tool-Worker-Pattern.md), [ADR-039](./ADR-039-Role-rethinking-and-intentions-routing.md), [ADR-041](./ADR-041-agents-roles-deprecation.md), [ADR-042](./ADR-042-Agents-Memory-Model-usage.md)

## 1. Context

The framework has two parallel tool-execution paths:

  1. **Legacy path** (`Tool` Protocol + `ToolInvoker`,
     ADR-005): a tool is a `Tool` (Protocol with
     `name`, `description`, `input_schema`, `invoke()`)
     and is invoked by the `ToolInvoker`, which reads
     `tool.<name>.requested` events from the EventLog
     and dispatches them to the registered `Tool`. The
     `ToolInvoker` runs **in-process** (in the same
     event loop as the dispatcher).

  2. **Worker path** (`@tool_worker` + `WorkerManager`,
     ADR-036): a tool is a class decorated with
     `@tool_worker(name=...)`. The `WorkerManager`
     consumes `tool.<name>.requested` events from a
     dedicated Redis Consumer Group queue and dispatches
     them in a `ProcessPoolExecutor` (across processes).
     This is the **canonical** path; ADR-036 made
     `WorkerManager` the recommended orchestrator.

Today, the **`LiteLLMTool`** (the bridge to LiteLLM and
to any provider LiteLLM supports) **lives on the legacy
path** (`Tool` Protocol, in-process, dispatched by
`ToolInvoker`). It is registered as `name="llm.complete"`
and is called **synchronously** by the `Role` classes
(`ChatRole.reply`, `PlannerRole.plan`,
`SummarizerRole.summarize`, `PersonalizedRole.respond`)
which themselves are called **synchronously** by the
`PlanAfterValidationSystem`-style systems.

This means:

  - **ADR-036 is not enforced for the LLM path.**
    Systems that call `await role.<verb>(...)` block
    the dispatcher event loop while the LLM responds.
    See `examples/04_reactive_system_with_llm.py:85`
    and `examples/05_session_chat.py:119` (the original
    imperative version).

  - **ADR-039 is not enforced for the `Role` classes.**
    `ChatRole.reply` is `async def` and calls
    `await self._invoke(...)` → `await self._llm.invoke(...)`.
    It is not a pure data component; it is an I/O-bearing
    role, which is what ADR-039 explicitly forbids.

  - **There is no cross-tick correlation for LLM calls.**
    The `ToolInvoker` runs the LLM in the same tick the
    `tool.<name>.requested` event lands. The
    `WorkerManager` (the new pattern) supports cross-tick
    correlation via `causation_id` (the `request_event_id`).
    A migrating `LiteLLMToolWorker` would gain this for
    free.

The user's request (2026-07-14): **migrate the LLM to
the `@tool_worker` pattern and deprecate the
`ToolInvoker`**.

## 2. Decision

We ship **`LiteLLMToolWorker`** (a `@tool_worker`
implementation of the LLM bridge) alongside the
existing `LiteLLMTool`. The legacy `LiteLLMTool` is
**deprecated** (1 release); the legacy `ToolInvoker`
is **deprecated** (2 releases; longer because it has
more users). The migration path is opt-in: existing
code that imports `LiteLLMTool` or `ToolInvoker`
continues to work, with a `DeprecationWarning` emitted
on import.

The `Role` classes (`ChatRole`, `PlannerRole`, etc.)
are **not** migrated in this ADR. The reason: the
`Role` → `System` migration is the work of a
follow-up ADR (likely ADR-044, building on ADR-039 and
ADR-041). Migrating the LLM call site is a 50-line
change in 4 roles; doing it in the same change as the
`@tool_worker` migration conflates two separate
concerns (the worker pattern vs the role-to-system
move). This ADR scopes the change to the **LLM
worker** only.

### 2.1 `LiteLLMToolWorker`

```python
# src/kntgraph/agents/tools/llm.py (new class)
@tool_worker(
    name="chat_llm",
    description="Generic LLM completion via LiteLLM.",
)
class LiteLLMToolWorker:
    """
    Migrated LiteLLM bridge: ADR-036 worker pattern.

    The legacy ``LiteLLMTool`` is kept for
    backwards-compat with examples 01-07. New code
    should use this worker via the WorkerManager.

    Same wire-format as the legacy tool
    (``idempotency_key`` is injected from
    ``request_event_id``). The result envelope is
    ``{"text": str, "usage": dict, "cost": float}`` so
    the system can introspect usage / cost in the
    completion phase.
    """

    def __init__(self) -> None:
        cfg = LLMConfig.from_env()
        self._transport = LiteLLMTransportAdapter(...)

    async def invoke(
        self,
        system: str,
        user: str,
        *,
        idempotency_key: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = False,
        response_format: dict | None = None,
        stream: bool = False,
    ) -> Result[dict[str, Any], Exception]:
        ...
```

### 2.2 Deprecation strategy

**`LiteLLMTool` (legacy) — 1 release:**

  - Emit `DeprecationWarning` on import (one-time per
    process, in the module-level code).
  - Add a class-level `__deprecated__ = True` marker
    so introspection tools can detect it.
  - Keep the implementation unchanged. The
    behaviour is identical; only the warning is new.
  - **Removal target:** v0.9.0 (next minor after
    v0.8.0).

**`ToolInvoker` (legacy) — 2 releases:**

  - Emit `DeprecationWarning` on import.
  - Add a class-level `__deprecated__ = True` marker.
  - The `ToolInvoker` continues to be the
    recommended path for tools that are NOT ready to
    be migrated to `@tool_worker` (e.g. the
    `PiiRedactionTool` and the example 11 use case).
  - **Removal target:** v1.0.0 (two minors after
    v0.8.0). Removal is gated on the audit: every
    registered `Tool` must either (a) be migrated
    to `@tool_worker` or (b) be marked "intentionally
    legacy" with a public reason.

### 2.3 Example update (05b)

`examples/05b_session_chat_ecs.py` (the WIP reference
implementation of ADR-042) currently uses
`MockChatLlmTool` (a `@tool_worker` mock). This ADR
replaces the mock with `LiteLLMToolWorker`, so the
example exercises the **real** LLM call path through
the WorkerManager. The mock stays in the file (commented
out) as a CI-test alternative when no LLM is available.

## 3. Consequences

### Positive

  - **ADR-036 enforced for the LLM path.** The
    `LiteLLMToolWorker` runs in the `WorkerManager`'s
    `ProcessPoolExecutor`; the dispatcher event loop
    is not blocked while the LLM responds.
  - **Cross-tick correlation.** The
    `tool.chat_llm.requested` event carries the
    `causation_id` (= the `user.intent` event id).
    A system can emit it in tick N and read the
    `tool.chat_llm.completed` in tick N+K (K = LLM
    latency). The legacy `ToolInvoker` ran in tick
    N only; if the LLM took more than the tick
    interval, the system would have to wait or
    poll.
  - **Clean migration path.** The legacy
    `LiteLLMTool` is unchanged in behaviour; the
    new `LiteLLMToolWorker` is a sibling. Callers
    can switch at their pace.
  - **Reusable result envelope.** The worker
    returns `{"text": str, "usage": dict, "cost": float}`,
    which is more useful for cost-tracking and
    metrics than the legacy `LLMResponse` Pydantic
    model. Systems can introspect `usage` and
    `cost` from the `tool_completion.data`.

### Negative

  - **Deprecation noise.** The
    `DeprecationWarning` on import will be visible
    in any code that imports `LiteLLMTool` or
    `ToolInvoker` (8 examples + 1 config + 1 src).
    We can suppress these with a
    `warnings.filterwarnings` once the examples are
    migrated; for now, the noise is intentional
    (signals the migration is happening).
  - **Two ways to do the same thing.** For the
    next release, both paths are supported. The
    risk is that new code copies the legacy path
    from existing examples. Mitigation: the
    `LiteLLMTool` docstring explicitly points to
    `LiteLLMToolWorker` as the recommended path; the
    example 01-07 are tagged "legacy" in their
    docstring until they are migrated.
  - **Process-pool overhead per LLM call.** The
    `WorkerManager` runs each LLM call in a
    `ProcessPoolExecutor` worker (fork + import +
    `__init__`). For a 200ms LLM call, the
    ~50ms of overhead is ~25% of the total
    latency. Mitigation: the
    `LiteLLMToolWorker.__init__` is light (loads
    the env config); the heavy work (HTTP, JSON)
    is in `invoke`. The overhead is amortised for
    chat workloads (the LLM call is the
    bottleneck).

### Mitigations

| Problem | Mitigation |
| --- | --- |
| Deprecation noise in CI | `warnings.filterwarnings("ignore", category=DeprecationWarning, module="kntgraph.agents.tools.llm\|kntgraph.agents.tools.invoker")` in `pyproject.toml::[tool.pytest.ini_options]`. |
| Two paths | The deprecation docstring on `LiteLLMTool` and `ToolInvoker` is explicit; the new example 05b is the reference. |
| Process-pool overhead | The `WorkerManager` already amortises the `__init__` cost; the LLM call dominates the latency. |

## 4. Migration Inventory

### Code (this commit)

  - `src/kntgraph/agents/tools/llm.py`:
    - Add `LiteLLMToolWorker` (new `@tool_worker`
      class, ~80 lines).
    - Add `DeprecationWarning` to `LiteLLMTool`
      (3 lines).
    - Add `__deprecated__ = True` marker on
      `LiteLLMTool`.
  - `src/kntgraph/agents/tools/invoker/__init__.py`:
    - Add `DeprecationWarning` to the module
      (2 lines).
  - `src/kntgraph/agents/tools/invoker/_invoker.py`:
    - Add `__deprecated__ = True` marker on
      `ToolInvoker` (1 line).
  - `examples/05b_session_chat_ecs.py`:
    - Replace `MockChatLlmTool` with
      `LiteLLMToolWorker` (5 lines changed).
    - Comment `MockChatLlmTool` as "CI-only"
      (kept for tests that don't have an LLM).
  - `DEBT.md`:
    - Add section 2.17 (this migration).
    - Add section 2.18 (the role migration
      follow-up).

### Follow-up PRs

  - **PR (v0.9.0):** Migrate the `Role` classes
    (`ChatRole`, `PlannerRole`, `SummarizerRole`,
    `PersonalizedRole`) to emit
    `tool.chat_llm.requested` instead of calling
    `LiteLLMTool` directly. The `Role` becomes a
    pure data component (ADR-039). Track as
    ADR-044.

  - **PR (v1.0.0):** Remove `LiteLLMTool` and
    `ToolInvoker`. Migrate the remaining examples
    (01-07) to use `LiteLLMToolWorker` via the
    `WorkerManager`. The audit checklist tracks
    every `Tool` registration; any that is not
    migrated must be marked "intentionally legacy"
    with a public reason.

## 5. Acceptance Checklist (Proposed → Accepted)

  - [ ] `LiteLLMToolWorker` exists with
    `@tool_worker(name="chat_llm")`.
  - [ ] `LiteLLMTool.invoke()` and the legacy
    `ToolInvoker.run_once()` continue to work
    unchanged (backwards-compat).
  - [ ] `DeprecationWarning` is emitted on
    import of `LiteLLMTool` and `ToolInvoker`.
  - [ ] Example 05b runs end-to-end against a
    real LLM (ollama or OpenAI).
  - [ ] `examples/01_llm_basic.py` continues to
    run (with a deprecation warning on the import).
  - [ ] `tests/unit/tools/test_invoker*.py`
    continue to pass.
  - [ ] New test
    `tests/unit/agents/tools/test_litellm_worker.py`
    exists and exercises the worker (mock the
    transport; no real LLM call).

When all boxes are checked, ADR-043 is `Accepted`
and the migration to the worker pattern is the
canonical path for the LLM.

## 6. Open Questions

1. **What is the worker name?** I propose
   `chat_llm` (matches the example 19 and 05b
   convention). Alternatives: `llm.complete`
   (matches the legacy name), `litellm.complete`.
   The name is locked at the protocol level
   (Redis key prefix `knt:tools:<name>:queue`)
   so changing it later is a breaking change.
   Decision: `chat_llm`. ADR-043 locked at this
   value; if a follow-up ADR changes the name,
   it must include a Consumer Group migration
   plan.
2. **What does the result envelope look like?**
   The legacy `LiteLLMTool.invoke()` returns a
   Pydantic `LLMResponse` (`text`, `usage`,
   `cost`). The worker envelope must be JSON-
   serialisable (it crosses the process boundary
   via Redis). Proposal: `dict[str, Any]` with
   the same three fields. The worker is a
   transport; the schema is the caller's concern
   (the `Role` class parses the `text` into a
   Pydantic model).
3. **Should the worker support streaming?**
   The legacy `LiteLLMTool` has an `astream()`
   method. The `@tool_worker` decorator
   supports a `stream` parameter; the worker
   could return a stream iterator. Defer to
   a follow-up ADR; for v0.8.0, streaming is
   out of scope (the worker returns the full
   text at the end, like the legacy non-stream
   path).
