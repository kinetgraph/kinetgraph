<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-047: Standardizing ToolWorker Construction via Adapters

**Status:** Draft (sync `ToolWorker` category ready for Accepted; `StreamsWorker` and cancellation tracked in §6)
**Date:** July 19, 2026
**Version:** 0.3.0 (sync ToolWorker category stable; StreamsWorker proposed in §6.1)
**Authors:** Architecture Team
**Related:** ADR-019-Epilogo-Typed-Adapters, ADR-034-ToolCall-ECS-Components, ADR-036-Tool-Worker-Pattern, ADR-037-Mandatory-Correlation-Propagation, ADR-044-Tool-call-Overlay-Accumulation, ADR-045-Tool-Call-Request-TTL, AGENTS.md

**Note on scope (v0.2.0):** The pattern described here applies to **ToolWorkers** (classes decorated with `@tool_worker` and executed by the `WorkerManager`), not to the `Tool` Protocol. The two are distinct: `Tool` is the identity/describable surface registered with the framework; `ToolWorker` is the executor. This ADR is about the executor's construction (how it reaches the outside world), not the identity. The "Tool" terminology in the body is shorthand for "ToolWorker" unless otherwise noted.

---

## 1. Context

In Kinetgraph, **ToolWorkers** (e.g., those decorated with `@tool_worker`) are responsible for executing external capabilities such as database queries, API calls, and third-party integrations. The `WorkerManager` instantiates each registered `ToolWorker` class inside a worker subprocess and invokes its `async def invoke(...)` method. The `invoke` method is the only execution surface; the `WorkerManager` handles event emission (`tool.<name>.requested` before, `tool.<name>.completed` or `tool.<name>.failed` after) and correlation propagation.

Currently, several concrete ToolWorkers import and instantiate third-party libraries directly within their class bodies or execution paths. This practice introduces direct coupling between the ToolWorker definition and concrete implementations, creating three primary architectural issues:

1. **Testability:** Mocking and stubbing external services becomes difficult, leading to heavy use of unit-test mocks (`unittest.mock.patch`) or requiring live services/containers in unit tests.
2. **Startup & Lifecycle Overhead:** Eagerly importing third-party libraries inside ToolWorker modules increases process initialization times and can lead to circular import dependencies.
3. **Inflexibility:** Swapping integrations (e.g., migrating from one payment gateway or email provider to another) requires modifying the ToolWorker's core logic rather than just swapping an adapter.

To ensure consistency with the framework's core (which abstracts all external libraries behind typed protocols, as detailed in ADR-019-Epilogo), we need a standardized pattern for constructing ToolWorkers that interact with external services.

---

## 2. Decision

We will establish a strict standard requiring all ToolWorker implementations to interact with external systems exclusively through **Service Adapters**.

This design introduces a clear separation between two distinct boundaries in the ToolWorker execution lifecycle:

1. **The Tool Protocol Boundary (Existing):** Governs how the framework's `WorkerManager` invokes a ToolWorker. The ToolWorker class carries identity metadata (`name`, `description`, `input_schema`) and exposes `async def invoke(...)`. The `WorkerManager` handles event emission and correlation propagation; the ToolWorker does not emit events directly.
2. **The Service Adapter Protocol Boundary (New/Proposed):** Governs how a ToolWorker invokes external I/O or libraries. Implemented by a separate Adapter class injected into the ToolWorker.

### 2.1 Architecture Diagram

```text
  [WorkerManager]
        │
        ▼  (Tool Protocol: invoke, name, description, input_schema)
  [ToolWorker] (Pure domain orchestration)
        │
        ▼  (Service Adapter Protocol: e.g., LLMTransport, ErpAdapter)
  [Service Adapter] (Translates to external libraries/calls)
        │
        ▼  (Third-party SDK / HTTP APIs)
  [External System] (e.g., LiteLLM, Stripe, SAP)
```

### 2.2 The Tool-Adapter Pattern Rules

1. **No Direct External Imports:** A ToolWorker class or module must never import, instantiate, or configure a concrete third-party client (e.g., `import stripe`, `import sap_client`).
2. **Abstract via Protocol:** All external service interactions must be defined behind a typed `Protocol` (e.g., `PaymentGatewayLike` or `ErpClient`).
3. **Dependency Injection:** The ToolWorker class must receive the adapter instance implementing the Protocol via its constructor (`__init__`).
4. **Adapter Reuse (when available):** If a suitable adapter Protocol already exists in the framework or vertical (e.g., `RedisLike`, `LLMTransport`, `EmbeddingProvider`), the ToolWorker SHOULD reuse it. A ToolWorker is also free to declare its own Protocol (e.g., `PaymentGatewayLike` for a new external service) — the pattern is **open**: reuse is the default for known adapters, but a new domain is a legitimate reason to introduce a new Protocol.
5. **Concrete Implementation Placement:** The concrete implementation of the adapter wrapping the external library must live in the infrastructure layer (`kntgraph.infra.<service>` or similar vertical-specific package) and use lazy/guarded imports to avoid startup overhead.
6. **Mock Testing:** Unit tests for the ToolWorker must inject a fake/stub implementation of the Protocol (e.g., `FakePaymentGateway`) instead of using mock patches or live external connections.

### 2.3 Resolving WorkerManager Constructor Constraints (Reusing Existing Adapters)

The framework's `WorkerManager` instantiates registered ToolWorker classes inside worker subprocesses using a zero-parameter constructor call (`tool_cls()`).

To satisfy this constraint while preserving dependency injection for tests, all ToolWorkers using service adapters (including framework-level ones like `LLMTransport`) MUST follow this instantiation pattern in their `__init__` constructor:

```python
def __init__(self, llm: LLMTransport | None = None) -> None:
    # 1. When instantiated in production by WorkerManager, 'llm' is None.
    # We fall back to the framework's existing LiteLLMTransportAdapter.
    # 2. When instantiated in tests, we pass a FakeLLMTransport stub.
    from kntgraph.agents.tools.llm import LiteLLMTransportAdapter

    self._llm = llm or LiteLLMTransportAdapter()
```

This ensures that:
- Production tool workers can be instantiated dynamically without arguments.
- Existing shared adapters (like `LiteLLMTransportAdapter`, `RedisEventLogAdapter`, or `OllamaEmbeddingAdapter`) are reused out-of-the-box.
- Heavy adapter modules are imported **lazily** inside `__init__`, preventing import overhead at startup.

**For new domains** (where no framework-level adapter exists), the constructor follows the same template but with a different Protocol and default adapter:

```python
def __init__(self, gateway: PaymentGatewayLike | None = None) -> None:
    from kntgraph.agents.tools.payments import StripePaymentAdapter
    self._gateway = gateway or StripePaymentAdapter()
```

### 2.4 Traceability & Correlation

The current framework contract is **bounded**:

- The `WorkerManager` emits `tool.<name>.requested` before invoking the ToolWorker and `tool.<name>.completed` / `tool.<name>.failed` after. The ToolWorker's `invoke` method **only** returns a `Result[Payload, ToolError]`; it does not emit events.
- Correlation is propagated by the `WorkerManager` automatically (ADR-037). The ToolWorker does not need to call `correlation_middleware.continue_from(...)`; the `idempotency_key` parameter IS the causation chain.

**Concretely, today (v0.7.0):**

1. **Causation binding** is structural: the `idempotency_key` parameter received in `invoke` is the `event_id` of the `tool.<name>.requested` event. Any side-effect the ToolWorker produces is causally linked to that request via the `causation_id` set by the `WorkerManager` on the completion event.
2. **Failure paths** come from two distinct sources, and the ToolWorker must distinguish them:
   - **Internal failure** (the ToolWorker's `invoke` returns `Err(ToolError)`): the `WorkerManager` translates this to `tool.<name>.failed` with `error_message` set from the `ToolError`.
   - **External failure** (the request never completes): per ADR-045, the `ToolCallTTLSweeperSystem` emits `tool.<name>.failed` with `error_message="ttl_expired"` if the `expires_at` of the request elapses without a completion. The ToolWorker's `invoke` may still return `Ok` afterward — the framework's slot accounting (ADR-044) is the single source of truth, not the ToolWorker's return value.
3. **Two Worker categories** are recognized in this pattern:
   - **`ToolWorker` (sync)**: returns a single `Result` from `invoke`. Sufficient for the common case (LLM, HTTP, DB queries).
   - **`StreamsWorker` (partial-completion)** (proposed — see §6.1): invoked N times under the same `causation_id`, each invocation producing one `StreamPartial`. The framework emits `tool.<name>.completed` with `partial=true` for each non-final partial, and `partial=false` for the terminal one. Cancellation is via the same `asyncio.Event` channel as the sync Worker (§6.2); the framework stops enqueueing partials after `cancellation.set()` and the in-flight partial returns `Err("cancelled")`. The model is **transport-agnostic**: local process pool today, gRPC/HTTP for remote workers tomorrow — both produce the same completion events, so the EventLog, slot accounting (ADR-044), and TTL eviction (ADR-045) work unchanged.
   Both categories share the same DI template (§2.3) and the same Protocol contract shape (see §6.4 — the base adapter response class).

**Implication for this ADR (§2.4):** the pattern guarantees correlation propagation **for what the framework currently emits** (request/completion/failure for `ToolWorker`; request/progress/completion/failure for `StreamsWorker`). The `ToolWorker` category is **ready now**; the `StreamsWorker` category is a tracked follow-up (§6.1) with a proposed shape.

---

## 3. Reference Implementation: Composing ToolWorkers over the LLM Adapter

To illustrate the flexibility of the Tool-Adapter pattern, we show how a single, low-level resource adapter (`LLMTransport`) is reused across multiple high-level, specialized ToolWorkers with distinct roles (Classification, Generation, and Image Analysis). These three examples all reuse the framework-level `LLMTransport` Protocol — the canonical case where rule §2.2.4 ("Adapter Reuse") applies.

### 3.1 Step 1: Reference the Adapter Protocol

The `LLMTransport` protocol is defined at the framework level in `src/kntgraph/tools/llm_transport.py`. It abstracts away LiteLLM/Ollama and takes an `LLMRequest` value object:

```python
# kntgraph/tools/llm_transport.py
from typing import Protocol, runtime_checkable
from dataclasses import dataclass

@dataclass(frozen=True)
class LLMRequest:
    model: str
    messages: list[dict]
    temperature: float = 0.0
    max_tokens: int = 1000
    idempotency_key: str = ""

@dataclass(frozen=True)
class LLMError:
    """Typed error channel for transport-level failures
    (rate limit, network, context overflow, etc.)."""
    kind: str       # e.g. "rate_limited", "context_overflow", "network"
    message: str

@dataclass(frozen=True)
class LLMResponse:
    """Discriminated envelope: success carries ``text``;
    failure carries ``error``. The Protocol returns
    this — not a raw dict."""
    success: bool
    text: str = ""
    error: "LLMError | None" = None

@runtime_checkable
class LLMTransport(Protocol):
    """Generic async boundary for making LLM completion requests.

    The return type is a typed envelope (success or error);
    raw dict access (e.g. ``res["choices"][0]``) is
    NOT part of the Protocol — the concrete adapter
    is responsible for the LiteLLM-to-envelope translation.
    """
    async def __call__(self, request: LLMRequest) -> LLMResponse: ...
```

**Why a typed envelope (not `dict`):** the ToolWorkers below (Section 3.2) consume the response. With `dict` returns, the ToolWorker has to know LiteLLM's wire format (`res["choices"][0]["message"]["content"]`) and reproduce the same KeyError-prone parsing. With a typed envelope, the Protocol is a clean boundary: the adapter translates, the ToolWorker consumes. This is a consequence of rule §2.2.2 ("Abstract via Protocol") — a Protocol that returns the third-party's native shape is not actually abstracting the third-party.

### 3.2 Step 2: Implement Distinct ToolWorkers Sharing the Adapter

Each ToolWorker implements a unique role by wrapping the same `LLMTransport` dependency and encapsulating its specific prompt engineering, parameter validation, and response formatting logic.

#### A. Classification ToolWorker
A ToolWorker that classifies user queries into a set of predefined labels.

```python
# kntgraph/agents/tools/classification.py
from typing import Any
from kntgraph.core.result import Ok, Err, Result, ToolError
from kntgraph.tools.worker import tool_worker
from kntgraph.tools.llm_transport import LLMTransport, LLMRequest

@tool_worker(name="intent_classifier", description="Classifies user intent.")
class IntentClassifierTool:

    def __init__(self, llm: LLMTransport | None = None, model: str = "gpt-4o-mini") -> None:
        from kntgraph.agents.tools.llm import LiteLLMTransportAdapter

        self._llm = llm or LiteLLMTransportAdapter()
        self._model = model

    async def invoke(
        self,
        text: str,
        categories: list[str],
        *,
        idempotency_key: str
    ) -> Result[dict[str, Any], ToolError]:
        prompt = f"Classify the text: '{text}' into categories: {categories}."
        request = LLMRequest(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=100,
            idempotency_key=idempotency_key
        )
        response = await self._llm(request)
        if not response.success:
            return Err(ToolError.from_llm_error(response.error))
        return Ok({"category": response.text.strip()})
```

#### B. Text Generation ToolWorker
A ToolWorker that generates custom content (e.g., summaries or email replies).

```python
# kntgraph/agents/tools/generation.py
from typing import Any
from kntgraph.core.result import Ok, Err, Result, ToolError
from kntgraph.tools.worker import tool_worker
from kntgraph.tools.llm_transport import LLMTransport, LLMRequest

@tool_worker(name="text_generator", description="Generates creative text content.")
class TextGeneratorTool:

    def __init__(self, llm: LLMTransport | None = None, model: str = "gpt-4o-mini") -> None:
        from kntgraph.agents.tools.llm import LiteLLMTransportAdapter

        self._llm = llm or LiteLLMTransportAdapter()
        self._model = model

    async def invoke(
        self,
        prompt: str,
        max_length: int = 500,
        *,
        idempotency_key: str
    ) -> Result[dict[str, Any], ToolError]:
        request = LLMRequest(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=max_length,
            idempotency_key=idempotency_key
        )
        response = await self._llm(request)
        if not response.success:
            return Err(ToolError.from_llm_error(response.error))
        return Ok({"text": response.text})
```

#### C. Multimodal Image Analysis ToolWorker
A ToolWorker that accepts an image payload (e.g., base64 or URI) and describes its content.

```python
# kntgraph/agents/tools/image_analysis.py
from typing import Any
from kntgraph.core.result import Ok, Err, Result, ToolError
from kntgraph.tools.worker import tool_worker
from kntgraph.tools.llm_transport import LLMTransport, LLMRequest

@tool_worker(name="image_analyzer", description="Analyzes and describes an image.")
class ImageAnalyzerTool:

    def __init__(self, llm: LLMTransport | None = None, model: str = "gpt-4o-mini") -> None:
        from kntgraph.agents.tools.llm import LiteLLMTransportAdapter

        self._llm = llm or LiteLLMTransportAdapter()
        self._model = model

    async def invoke(
        self,
        image_base64: str,
        question: str = "Describe this image",
        *,
        idempotency_key: str
    ) -> Result[dict[str, Any], ToolError]:
        # Formulate multimodal messages structure for the adapter
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                }
            ]
        }]

        request = LLMRequest(
            model=self._model,
            messages=messages,
            temperature=0.2,
            max_tokens=300,
            idempotency_key=idempotency_key
        )
        response = await self._llm(request)
        if not response.success:
            return Err(ToolError.from_llm_error(response.error))
        return Ok({"description": response.text})
```

**A note on the "no progress events" stance (§2.4):** the three ToolWorkers above all return a single `Result` at the end of `invoke`. A long-running streaming variant (e.g., a streaming LLM response) is not expressible as a sync `ToolWorker`. The framework's transport-agnostic answer is the `StreamsWorker` partial-completion model (§6.1): the streaming case is the same Worker invoked N times under the same `causation_id`, with each invocation producing one chunk. The Worker is stateless across partials from the framework's point of view, so the same code runs on a local process pool or on remote gRPC workers.

---

## 4. Consequences

### Pros

- **Decoupled Architecture:** ToolWorkers remain pure domain orchestrators, completely separated from third-party library details.
- **Adapter Reusability:** Low-level integrations (like `LLMTransport` and its caching/fallback middleware) are implemented once and shared across multiple ToolWorkers.
- **Fast and Local Testing:** Testing any of these ToolWorkers requires no live LLM connections; simply pass an in-memory `FakeLLMTransport` that returns pre-configured envelopes.
- **Pluggability within the Protocol:** The backend implementation of an adapter can be swapped (e.g., a `LiteLLMTransportAdapter` replaced by an `OllamaTransportAdapter`) without changing the ToolWorker class, as long as the new adapter satisfies the same `LLMTransport` Protocol. The ToolWorker is **not** decoupled from the Protocol itself — switching to a fundamentally different external service (e.g., from LLM to a rules engine) requires a new Protocol and a new ToolWorker (or an adapter that emulates the old shape).

### Cons

- **Slight Indirection:** Adds a Protocol and delegation step to every external call, slightly increasing boilerplate code when first building a ToolWorker.
- **Strict Architecture Discipline:** Developers must design the Protocol interface explicitly, which requires more upfront thinking compared to importing a client directly.
- **Two categories, not one:** ToolWorkers come in two flavours (sync `ToolWorker` and partial-completion `StreamsWorker` — see §6.1). The dev must pick the right one per Worker. A sync Worker is never upgraded to a stream (different `@streams_worker` decorator, different `invoke` signature with `partial_seq`/`partial_id`); a `StreamsWorker` pays a round-trip per partial, so a single-shot use case should stay sync.

---

## 5. Recommendation

We recommend adopting this standard for all **new** sync `ToolWorker` development immediately. The `ToolWorker` (sync) category is well-defined and can move from **Draft** to **Accepted** once the review of §2 (especially §2.2.4 — Adapter Reuse) and §3 (the LLM reference implementation) is complete.

The `StreamsWorker` category (§6.1), the sync cancellation channel (§6.2), and the `AdapterResponse` base class (§6.4) are tracked as proposed follow-ups. They will be formalized in a separate ADR (e.g., ADR-049 "Streaming, Cancellation, and Adapter Responses for ToolWorkers") when the first concrete use case lands — a streaming LLM Worker is the most likely first adopter, and its transport target (local pool vs. remote gRPC) is a deployment choice the framework should not preempt.

Existing ToolWorkers that directly import external dependencies should be refactored to conform to the Tool-Adapter pattern in future sprints, logging their refactoring targets in `DEBT.md`.

---

## 6. Open Questions

### 6.1 `StreamsWorker` — partial-completion model (transport-agnostic)

**Problem.** Long-running external calls (streaming LLM, batch transcode, large document parse) need to surface intermediate state. The current `ToolWorker.invoke -> Result` shape forces the caller to wait for the full completion or use TTL eviction to detect abandonment.

**Why not an async iterator.** An `AsyncIterator[Result[Partial, ToolError]]` returned from `invoke` is **process-local**: the framework iterates the iterator in the same process where the Worker ran. This couples the dispatch loop to the Worker process for the entire stream duration. As we scale to client-server with **remote workers** (a future deployment shape — workers on separate machines, behind a queue, possibly in another region), the iterator has to be reified over the wire: every `__anext__` becomes a round-trip to the remote Worker, the Worker must hold the iterator state, and the framework gains a stateful bidirectional channel per in-flight stream. That is a different transport with its own failure modes (mid-stream disconnects, partial result loss, server-side iterator lifecycle).

**Proposal (partial-completion model).** Treat a stream as **N sequential invocations of the same Worker**, each producing one completion event. The framework already knows how to enqueue a request, poll the EventLog, and route a completion back to a system. A `StreamsWorker` is just a `ToolWorker` whose `invoke` is **invoked multiple times** under the same `causation_id`, with each invocation contributing one chunk of the final result:

```
request: tool.streaming_chat.requested
        │
        ▼
   ┌─ partial 1 ─┐    ┌─ partial 2 ─┐    ┌─ partial N (final) ─┐
   │ invoke()    │    │ invoke()    │    │ invoke()            │
   │ → partial  │    │ → partial  │    │ → final             │
   └─────┬──────┘    └─────┬──────┘    └─────────┬──────────┘
         ▼                  ▼                     ▼
   tool.streaming_chat.completed  (partial: true,  seq: 1)
   tool.streaming_chat.completed  (partial: true,  seq: 2)
   tool.streaming_chat.completed  (partial: false, seq: N)
```

The completion event payload gains a `partial: bool` flag and an auto-incremented `seq`. The framework does not change: each invocation is a regular `tool.<name>.requested` → `tool.<name>.completed` round trip, with the `causation_id` of the original request shared across the sequence. The slot accounting (ADR-044) and TTL eviction (ADR-045) work as today — the request stays in `tool_requests` until the **final** completion lands.

**Worker contract.** The `invoke` method receives a `partial_seq: int` parameter (the sequence number of the partial being requested) and a `partial_id: str | None` (an opaque cursor the Worker returns at the end of each partial so it can resume state on the next call). The Worker is **stateless across partials** from the framework's point of view — it persists whatever it needs (via the adapter Protocol, e.g., a Redis-backed `StreamStateStore`) and reads it back on the next call:

```python
from kntgraph.tools.worker import streams_worker
from kntgraph.tools.llm_transport import LLMTransport, LLMRequest

@streams_worker(name="streaming_chat", description="Streaming chat completion.")
class StreamingChatWorker:

    def __init__(self, llm: LLMTransport | None = None) -> None:
        from kntgraph.agents.tools.llm import LiteLLMTransportAdapter
        self._llm = llm or LiteLLMTransportAdapter()

    async def invoke(
        self,
        prompt: str,
        *,
        idempotency_key: str,
        partial_seq: int,
        partial_id: "str | None" = None,
        cancellation: asyncio.Event,
    ) -> Result["StreamPartial", ToolError]:
        if cancellation.is_set():
            return Err(ToolError("cancelled"))

        # Resume from the previous partial's cursor, or
        # start fresh. The Worker owns the state shape.
        cursor = await self._state.load(partial_id) if partial_id else None

        # Compute the next chunk (e.g. next N tokens
        # from the LLM stream, or the next 1MB of a
        # transcode output). The Worker decides the
        # chunk size; the framework imposes no latency
        # target.
        chunk, next_cursor, is_final = await self._next_chunk(
            prompt, cursor, cancellation
        )

        # Persist the cursor so the next partial call
        # can resume. The state store is itself an
        # adapter Protocol (StorageLike) — no direct
        # Redis imports.
        new_partial_id = await self._state.save(next_cursor)

        return Ok(StreamPartial(
            chunk=chunk,
            partial_id=new_partial_id,
            is_final=is_final,
        ))
```

**Framework adaptation rules:**

| Worker returns | Framework event |
| --- | --- |
| `Ok(partial)` where `is_final=False` | `tool.<name>.completed` with `partial=True`, `seq=partial_seq` |
| `Ok(partial)` where `is_final=True` | `tool.<name>.completed` with `partial=False`, `seq=partial_seq` (terminal — slot evicted per ADR-044) |
| `Err(error)` | `tool.<name>.failed` (terminal — slot evicted per ADR-044) |
| Worker does not enqueue next partial within `partial_timeout_s` | `tool.<name>.failed` with `error_message="partial_timeout"` (per ADR-045 sweeper logic) |
| `tool.<name>.cancel_requested` event in the EventLog | `WorkerManager` calls `cancellation.set()`; the current partial returns `Err("cancelled")`; no further partials are enqueued |

**Chunk size is the Worker's choice.** A streaming LLM Worker may produce 1 partial per N tokens (say 50) to balance EventLog volume against visible progress. A batch transcode Worker may produce 1 partial per percentage point of progress. A document parse Worker may produce 1 partial per chapter. The framework does not impose a granularity — the Worker trades EventLog write cost against progress visibility per its domain. The reference implementation in `agents/tools/streaming_chat.py` (deferred) will document a default of "1 partial per worker invocation, max 1 invocation per `partial_timeout_s`".

**Why this works at any transport.** The `WorkerManager` is the only thing that knows the transport (process pool today, gRPC/HTTP tomorrow). The Worker contract is "call `invoke(prompt, ..., partial_seq, partial_id, cancellation)` and return one chunk". Local workers round-trip via function call; remote workers round-trip via gRPC; both produce the same completion event. The EventLog is the source of truth, and `causation_id` chains partials to the original request. **Mid-stream disconnect is recoverable**: if the worker process dies after partial 3, the framework sees `partial_timeout` and emits `failed`; the system can decide to retry the entire stream or surface a partial result.

**Status:** **proposed** for the next minor cycle. Tracking target: alongside the `ToolCallTTL` rework (ADR-045 follow-up) so the partial timeout and the request TTL share the same dispatcher hooks. The `WorkerManager` change (enqueue-partial loop, `partial_timeout_s` knob, cancel routing) is the main piece of plumbing; the Protocol contract above is the recommendation and will move to a separate ADR (e.g., ADR-049 "Streaming and Cancellation for ToolWorkers") when the first concrete use case lands.

### 6.2 Cancellation of sync `ToolWorker`s

**Problem.** A sync `ToolWorker.invoke` awaiting I/O cannot be interrupted cleanly. The TTL sweeper (ADR-045) only marks the request as failed **after** `expires_at` — by then the worker's `await` may have already returned, and the worker is left in an unknown state.

**Direction (per discussion).** Cancellation can come from **two sources**:

1. **System-driven** (top-down): a downstream system emits a `tool.<name>.cancel_requested` event. The `WorkerManager` matches it to the in-flight request and signals the worker.
2. **Manager-driven** (bottom-up): the `WorkerManager` decides to cancel — e.g., worker process is being recycled, dispatcher is shutting down, or a higher-priority request preempts.

Both paths converge on the same worker-side signal.

**Proposed shape (option a — explicit channel):** inject a `cancellation: asyncio.Event` parameter into `invoke`. The ToolWorker checks it between awaits. Cleaner than wrapping the whole `invoke` in a task (option b from the earlier draft) because:

- No risk of `asyncio.CancelledError` leaking into the worker's exception path and being caught by an over-broad `except Exception`.
- The worker controls *when* it checks (between I/O calls, after sub-steps), not the framework.
- The same `asyncio.Event` is reused for `StreamsWorker` if needed (though `aclose()` is preferred there).

```python
async def invoke(
    self, prompt: str, *, idempotency_key: str, cancellation: asyncio.Event
) -> Result[dict, ToolError]:
    if cancellation.is_set():
        return Err(ToolError("cancelled_before_start"))
    # ... do work, checking cancellation.is_set() between awaits ...
    if cancellation.is_set():
        return Err(ToolError("cancelled"))
    return Ok(...)
```

**Status:** **proposed**, coupled to §6.1. The sync `ToolWorker` and the `StreamsWorker`'s per-partial invocation share the same `asyncio.Event` parameter; the framework-side plumbing is the same: a cancellation source (system event or manager decision) maps to `event.set()`, and the Worker returns `Err("cancelled")` at the next check. For `StreamsWorker`, the framework also stops enqueueing the next partial after the current one finishes.

### 6.3 Partial / streaming return shape (subsumed by §6.1)

The earlier draft listed this as a separate question. With the partial-completion model adopted (§6.1), partial / streaming return is **fully covered**: each `invoke` call produces one `StreamPartial`; the framework emits a `partial=true` completion for non-final partials and `partial=false` for the terminal one. The Worker's `Partial` type is the domain's choice (string, percentage, bytes, etc.). No separate question needed.

**Status:** **resolved by §6.1.**

### 6.4 Base adapter response class for typed envelopes

**Problem.** §3.1 introduced `LLMResponse` and `LLMError` for the LLM case. The question: should every Protocol in the framework follow the same discriminated shape, or is the LLM case special?

**Proposal (option a from earlier draft, now adopted as the recommended shape).** Provide a generic base class in the framework that Protocols compose or subclass. The dev decides the granularity per domain — the base class is a **scaffolding helper**, not a constraint.

```python
# kntgraph/tools/adapter_response.py (proposed)
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

@dataclass(frozen=True, slots=True)
class AdapterError:
    kind: str        # "rate_limited" | "context_overflow" | "network" | ...
    message: str

@dataclass(frozen=True, slots=True)
class AdapterResponse(Generic[T]):
    """Discriminated envelope for adapter returns.

    The dev may use ``AdapterResponse`` directly for
    simple cases (one success payload, one error kind)
    or subclass for richer domain shapes (LLM needs
    ``usage_tokens``, payment needs ``transaction_id``,
    etc.).
    """
    success: bool
    value: "T | None" = None
    error: "AdapterError | None" = None

    @classmethod
    def ok(cls, value: T) -> "AdapterResponse[T]":
        return cls(success=True, value=value)

    @classmethod
    def err(cls, kind: str, message: str) -> "AdapterResponse[T]":
        return cls(success=False, error=AdapterError(kind=kind, message=message))

    def unwrap(self) -> T:
        if not self.success:
            raise ValueError(f"unwrap on Err: {self.error}")
        return self.value  # type: ignore[return-value]

    def unwrap_or(self, default: T) -> T:
        return self.value if self.success else default
```

A simple adapter uses it as-is:

```python
@runtime_checkable
class PaymentGatewayLike(Protocol):
    async def charge(self, amount: int) -> AdapterResponse[dict]: ...
```

A richer domain (LLM) subclasses for the extra fields:

```python
@dataclass(frozen=True, slots=True)
class LLMResponse(AdapterResponse[str]):
    """LLM-specific response. Extends the base with
    usage telemetry and finish reason."""
    usage_tokens: int = 0
    finish_reason: str = "stop"
```

**Status:** **proposed**. Lightweight, opt-in, no impact on existing Protocols. A future ADR (e.g., ADR-049 "Standardized Adapter Response Shape") can formalize the migration of `LLMTransport`, `EmbeddingProvider`, and other Protocols to use the base class.

### 6.5 Summary of open items

| # | Item | Status | Blocking ADR acceptance? |
| --- | --- | --- | --- |
| 6.1 | `StreamsWorker` (partial-completion model, transport-agnostic) | proposed | No (the sync `ToolWorker` category is sufficient today) |
| 6.2 | Sync `ToolWorker` cancellation channel | proposed, coupled to 6.1 | No (TTL is the current termination signal) |
| 6.3 | Partial / streaming return shape | resolved by 6.1 | — |
| 6.4 | `AdapterResponse` base class | proposed | No (opt-in scaffolding) |

This ADR can move from **Draft** to **Accepted** for the sync `ToolWorker` category once the review above is incorporated. The `StreamsWorker`, cancellation, and adapter response work will be tracked in a follow-up ADR (e.g., ADR-049 "Streaming, Cancellation, and Adapter Responses for ToolWorkers") when the first concrete use case lands.
