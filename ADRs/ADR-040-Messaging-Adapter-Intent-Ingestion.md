<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-040: Messaging Adapter for Intent Ingestion (CLI Scaffolding vs. Generic Framework Gateway)

**Status:** Proposed

**Date:** July 11, 2026

**Authors:** Architecture Team

**Related:** [ADR-012](./ADR-012-IntentRouter-HTTP-Gateway.md), [ADR-017](./ADR-017-Identity-Authorizationmodel.md), [ADR-037](./ADR-037-Mandatory-Correlation-Propagation.md), [ADR-038](./ADR-038-CLI-Boilerplate-Generator.md)

---

## 1. Context

Kinetgraph introduces external events into the system through Gateway Adapters (e.g., [ADR-012](./ADR-012-IntentRouter-HTTP-Gateway.md) HTTP Gateway). Currently, the CLI provides a `--use-intent-http` flag during `knt init` to bootstrap a FastAPI-based HTTP gateway that verifies API keys, initializes `CorrelationContext`, and appends intents to the `EventLog`.

However, in many production environments, user intents are ingested asynchronously from messaging queues (e.g., RabbitMQ, Apache Kafka, AWS SQS, or Redis Streams) instead of síncronous HTTP endpoints. 

To support messaging-based ingestion, we must choose between two design strategies:

1. **Option A: Generic and Configurable Messaging Gateway in the Framework Core.**
   The framework provides a base abstract consumer or direct drivers for SQS/RabbitMQ/Kafka, configured via env vars.
2. **Option B: CLI Scaffolding Template (`--use-intent-messaging` / `--use-intent-pubsub`).**
   The CLI generates boilerplate code (`consumer.py`) that shows how to bind to a messaging queue, handle connection loops, parse payloads, resolve security `Principal`, propagate `CorrelationContext`, and append to `EventLog`.

---

## 2. Decision

We choose **Option B: CLI Scaffolding Template (`--use-intent-messaging`)**.

We will not build generic messaging drivers into the framework core. Instead, Kinetgraph will provide a CLI scaffolding flag `--use-intent-messaging` that generates a clean, customizable messaging consumer loop in the user's application workspace.

### 2.1 Rationale for Option B

1. **Zero Dependency Bloat:** 
   Messaging libraries (like `confluent-kafka`, `aio-pika`, `boto3`) are heavy and carry native C-binding dependencies. Forcing these packages onto the core `kntgraph` framework would drastically increase installation footprint and dependency conflicts for users who do not need them.
2. **Infinite Protocol/Transport Variation:** 
   Every messaging queue has unique features:
   - **Serialization:** Protobuf, Avro, JSON, or custom headers.
   - **Authentication:** IAM roles, SASL/SCRAM, SSL client certs.
   - **Error Handling:** manual ACKs, dead-letter exchanges (DLX), exponential backoff retries.
   A generic configurable abstraction in the framework would inevitably become a "lowest common denominator" wrapper, frustrating developers who need to configure advanced transport features.
3. **Clean Architecture / Boundary Separation:** 
   In DDD, message ingestion is an **Infrastructure Adapter** that sits on the boundary of the Bounded Context. Generating a template in the application space gives the developer complete ownership of the consumer loop, allowing them to hook up custom tracing, custom metrics, and custom error boundaries natively.

## 3. Scaffolding Specifications

We support three project scaffolding options during `knt init` via flags combinations:

1. **HTTP Only Ingestion (`--use-intent-http`):**
   - The CLI generates a `main.py` bootstrapped with a FastAPI server exposing HTTP API routes (`/api/v1/intents`) to ingest user intents.
   - Ideal for standard REST-driven synchronous applications.

2. **Messaging Only Ingestion (`--use-intent-messaging`):**
   - The CLI generates a `consumer.py` containing the background message consumer.
   - The `main.py` entrypoint still bootstraps a minimal FastAPI HTTP server, not for intent ingestion, but to expose crucial application lifecycle endpoints:
     - Health checks (`/healthz`, `/readyz`).
     - Observability metrics (`/metrics`).
     - Agent / Application state inspection.
   - User intents are exclusively ingested asynchronously via the background message listener.

3. **Hybrid Ingestion (`--use-intent-http --use-intent-messaging`):**
   - The CLI scaffolds both the FastAPI HTTP routes for intent ingestion and the async background message consumer.
   - In `main.py`, the `lifespan` manager spins up the background consumer loop alongside the dispatchers, letting the application ingest intents from HTTP and message queues concurrently into the same unified `EventLog`.

When `--use-intent-messaging` is provided, the CLI will generate `src/my_project/consumer.py` with the following template structure:

```python
import asyncio
import json
import logging
import uuid
from typing import Any

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.stream.event_log import EventLog
from kntgraph.security import Principal, Role

logger = logging.getLogger(__name__)

class IntentMessageConsumer:
    """
    Template messaging consumer.
    Customise this loop to bind to your queue of choice (RabbitMQ, SQS, Kafka, etc.).
    """

    def __init__(self, log: EventLog) -> None:
        self._log = log

    async def start(self) -> None:
        logger.print("Starting Intent Message Consumer loop...")
        # Placeholder for connection logic
        # connection = await connect_queue()
        
        while True:
            try:
                # 1. Fetch message from queue (Replace with actual queue read)
                # message = await queue.get()
                await asyncio.sleep(1.0)
                continue
                
                # 2. Extract metadata
                # payload = json.loads(message.body)
                # correlation_id = message.headers.get("x-correlation-id") or str(uuid.uuid4())
                # agent_id = payload["agent_id"]
                # tenant_id = payload.get("tenant_id")
                # intent_name = payload["intent"]
                # params = payload.get("params", {})
                
                # 3. Setup context (L2 Principal & Correlation Context)
                # principal = Principal(
                #     agent_id=agent_id,
                #     role=Role.agent,
                #     tenant_id=tenant_id,
                #     key_id="messaging-gateway"
                # )
                # correlation = CorrelationContext.new(correlation_id=uuid.UUID(correlation_id))
                
                # 4. Create and append the intent event
                # intent_event = Event.domain_from(
                #     agent_id=agent_id,
                #     type=f"intent.{intent_name}.received",
                #     data={
                #         "target_tool": payload.get("target_tool"),
                #         "parameters": params,
                #         "status": "pending",
                #     },
                #     correlation=correlation
                #     # principal_ctx bound at log.append context
                # )
                # await self._log.append(intent_event)
                
                # 5. Acknowledge message
                # await message.ack()
                
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                # Handle reject/NACK/DLQ logic here
```

---

## 4. Consequences

### Pros
* **Ultimate Flexibility:** Developers have complete control over queue subscription, parsing, acking, and retry/DLQ patterns.
* **Keep Core Lightweight:** No external messaging queue client libraries are added to the Kinetgraph package installation dependencies.
* **Educational:** The generated template acts as documentation, explicitly showing how L2 Principals, CorrelationContext, and EventLog ingestion should be wired together for messaging.

### Cons
* **Boilerplate Maintenance:** The user is responsible for writing and maintaining the actual connection client loop (e.g. implementing the `connect_queue()` logic).
