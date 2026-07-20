<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-047: Standardizing Tool Construction via Adapters

**Status:** Proposed

**Date:** July 19, 2026

**Version:** 0.1.0

**Authors:** Architecture Team

**Related:** ADR-019-Epilogo-Typed-Adapters, ADR-036-Tool-Worker-Pattern, ADR-037-Mandatory-Correlation-Propagation, AGENTS.md

---

## 1. Context

In Kinetgraph, tools (e.g., those decorated with `@tool_worker` and implementing the `Tool` protocol) are responsible for executing external capabilities such as database queries, API calls, and third-party integrations.

Currently, several concrete tools or tool-workers import and instantiate third-party libraries directly within their class bodies or execution paths. This practice introduces direct coupling between the tool definition and concrete implementations, creating three primary architectural issues:

1. **Testability:** Mocking and stubbing external services becomes difficult, leading to heavy use of unit-test mocks (`unittest.mock.patch`) or requiring live services/containers in unit tests.
2. **Startup & Lifecycle Overhead:** Eagerly importing third-party libraries inside tool modules increases process initialization times and can lead to circular import dependencies.
3. **Inflexibility:** Swapping integrations (e.g., migrating from one payment gateway or email provider to another) requires modifying the tool's core logic rather than just swapping an adapter.

To ensure consistency with the framework's core (which abstracts all external libraries behind typed protocols, as detailed in ADR-019-Epilogo), we need a standardized pattern for constructing tools that interact with external services.

---

## 2. Decision

We will establish a strict standard requiring all tool implementations to interact with external systems exclusively through **Service Adapters**.

This design introduces a clear separation between two distinct boundaries in the tool execution lifecycle:

1. **The Tool Protocol Boundary (Existing):** Governs how the framework's Dispatcher/Runner invokes a tool. Implemented by the Tool class (typically decorated with `@tool_worker`).
2. **The Service Adapter Protocol Boundary (New/Proposed):** Governs how a tool invokes external I/O or libraries. Implemented by a separate Adapter class injected into the Tool.

### 2.1 Architecture Diagram

```text
  [Dispatcher / Runner] 
           │
           ▼  (Tool Protocol: invoke, name, description, input_schema)
     [Tool Worker] (Pure domain orchestration)
           │
           ▼  (Service Adapter Protocol: e.g., LLMTransport, ErpAdapter)
    [Service Adapter] (Translates to external libraries/calls)
           │
           ▼  (Third-party SDK / HTTP APIs)
   [External System] (e.g., LiteLLM, Stripe, SAP)
```

### 2.2 The Tool-Adapter Pattern Rules

1. **No Direct External Imports:** A Tool class or module must never import, instantiate, or configure a concrete third-party client (e.g., `import stripe`, `import sap_client`).
2. **Abstract via Protocol:** All external service interactions must be defined behind a typed `Protocol` (e.g., `PaymentGatewayLike` or `ErpClient`).
3. **Dependency Injection:** The Tool class must receive the adapter instance implementing the Protocol via its constructor (`__init__`).
4. **Adapter Reuse:** If a suitable adapter Protocol already exists in the framework or vertical (e.g., `RedisLike`, `LLMTransport`, `EmbeddingProvider`), the tool must reuse it.
5. **Concrete Implementation Placement:** The concrete implementation of the adapter wrapping the external library must live in the infrastructure layer (`kntgraph.infra.<service>` or similar vertical-specific package) and use lazy/guarded imports to avoid startup overhead.
6. **Mock Testing:** Unit tests for the tool must inject a fake/stub implementation of the Protocol (e.g., `FakePaymentGateway`) instead of using mock patches or live external connections.

### 2.3 Resolving WorkerManager Constructor Constraints (Reusing Existing Adapters)

The framework's `WorkerManager` instantiates registered tool classes inside worker subprocesses using a zero-parameter constructor call (`tool_cls()`).

To satisfy this constraint while preserving dependency injection for tests, all tools using service adapters (including framework-level ones like `LLMTransport`) MUST follow this instantiation pattern in their `__init__` constructor:

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

### 2.4 Traceability & Correlation for Slow / Long-Running Tasks

For slow or long-running tasks executed asynchronously by the worker manager (e.g. batch calculations, complex LLM flows, multi-second I/O), preserving traceability is mandatory (ADR-037).

1. **Correlation Context Propagation:** Any event emitted by a tool (e.g., intermediate progress events, status updates, or nested lifecycle logs) must carry the `CorrelationContext` of the triggering flow.
2. **Causation Binding:** The `idempotency_key` keyword parameter supplied to the tool's `invoke` method is structurally the `event_id` of the `tool.{name}.requested` event. This serves as the `causation_id` for any side-effects.
3. **Traceability in Progress Events:** If a long-running tool worker needs to emit intermediate progress updates (e.g. `tool.{name}.progress_updated`), it must:
   - Recover the triggering event context or correlation context.
   - Use `correlation_middleware.continue_from(trigger_event)` to chain the progress event to the flow lineage.
   - This ensures full auditability across boundaries: `user.intent (Correlation X) -> tool.requested (Causation Y) -> tool.progress_updated (Correlation X, Causation Y) -> tool.completed (Correlation X, Causation Y)`.

---

## 3. Reference Implementation: Composing Tools over the LLM Adapter

To illustrate the flexibility of the Tool-Adapter pattern, we show how a single, low-level resource adapter (`LLMTransport`) is reused across multiple high-level, specialized tools with distinct roles (Classification, Generation, and Image Analysis).

### 3.1 Step 1: Reference the Adapter Protocol

The `LLMTransport` protocol is defined at the framework level in `src/kntgraph/tools/llm_transport.py`. It abstracts away LiteLLM/Ollama and takes an `LLMRequest` value object:

```python
# kntgraph/tools/llm_transport.py
from typing import Protocol, runtime_checkable
from .llm_transport import LLMRequest

@runtime_checkable
class LLMTransport(Protocol):
    """Generic async boundary for making LLM completion requests."""
    async def __call__(self, request: LLMRequest) -> dict:
        ...
```

### 3.2 Step 2: Implement Distinct Tools Sharing the Adapter

Each tool implements a unique role by wrapping the same `LLMTransport` dependency and encapsulating its specific prompt engineering, parameter validation, and response formatting logic.

#### A. Classification Tool
A tool that classifies user queries into a set of predefined labels.

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
        try:
            res = await self._llm(request)
            classification = res["choices"][0]["message"]["content"].strip()
            return Ok({"category": classification})
        except Exception as e:
            return Err(ToolError.from_exception(e))
```

#### B. Text Generation Tool
A tool that generates custom content (e.g. summaries or email replies).

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
        try:
            res = await self._llm(request)
            output = res["choices"][0]["message"]["content"]
            return Ok({"text": output})
        except Exception as e:
            return Err(ToolError.from_exception(e))
```

#### C. Multimodal Image Analysis Tool
A tool that accepts an image payload (e.g., base64 or URI) and describes its content.

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
        try:
            res = await self._llm(request)
            description = res["choices"][0]["message"]["content"]
            return Ok({"description": description})
        except Exception as e:
            return Err(ToolError.from_exception(e))
```

---

## 4. Consequences

### Pros

- **Decoupled Architecture:** Tools remain pure domain orchestrators, completely separated from third-party library details.
- **Adapter Reusability:** Low-level integrations (like `LLMTransport` and its caching/fallback middleware) are implemented once and shared across multiple tools.
- **Fast and Local Testing:** Testing any of these tools requires no live LLM connections; simply pass an in-memory `FakeLLMTransport` that returns pre-configured dictionaries.
- **Pluggability:** Changing the backend technology (e.g., from LiteLLM to a custom HTTP gateway or mock service) only requires writing a new adapter implementation; all tool classes remain unchanged.

### Cons

- **Slight Indirection:** Adds a Protocol and delegation step to every external call, slightly increasing boilerplate code when first building a tool.
- **Strict Architecture Discipline:** Developers must design the Protocol interface explicitly, which requires more upfront thinking compared to importing a client directly.

---

## 5. Recommendation

We recommend adopting this standard immediately for all new tool development. Existing tools that directly import external dependencies should be refactored to conform to the Tool-Adapter pattern in future sprints, logging their refactoring targets in `DEBT.md`.
