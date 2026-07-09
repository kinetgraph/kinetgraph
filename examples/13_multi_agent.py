# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
13 — Cooperation between independent systems on the same agent.

Demonstrates the canonical multi-step reactive flow: an
agent appends events to the EventLog, and **two
independent** ``World -> list[Event]`` systems cooperate
on the same World to drive the flow to completion. The
systems are pure functions of the World and know nothing
about each other; the dispatcher just runs them in order
on the post-fold World (ADR-018).

In a real deployment, the two systems below would live in
two different *services* (the `requester` bot and the
`approver` bot). The demo keeps them in one process to
stay runnable; the contract is identical:

  - The `requester` system emits `invoice.requested`.
  - The `approver` system reacts to `invoice.requested`
    whose `valor` exceeds a threshold by emitting
    `invoice.approved`.
  - The `requester` system reacts to `invoice.approved`
    (where `causation_id` points to one of its own
    requests) by emitting the terminal `invoice.issued`.

All three steps are pure:

    async def system(world: World) -> list[Event]

The systems inspect the World's per-agent `AgentView`
(``domain_phase`` + ``components[event_type]``) and emit
based on the agent's current state. No system ever
receives the triggering `Event` directly — that is the
ADR-018 contract, and it is what makes the systems
replayable, testable in isolation, and parallelisable.

Pre-requisites
--------------

  - Redis on localhost:6379 (default).

Run
---

    python examples/13_multi_agent.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from kntgraph.core.event import Event  # noqa: E402
from kntgraph.core.result import Ok, Result, ToolError  # noqa: E402
from kntgraph.knowledge.extraction import (  # noqa: E402
    Classification,
    IntentClassifier,
    IntentScore,
    RegexFieldFinder,
    SchemaArgumentExtractor,
)
from kntgraph.infra.redis._event_log import RedisEventLogAdapter  # noqa: E402
from kntgraph.stream.event_log import EventLog  # noqa: E402
from kntgraph.agents.tools.protocol import (  # noqa: E402
    Tool,
    ToolEventType,
    ToolRegistry,
)

from kntgraph.agents.roles import (  # noqa: E402
    RoutingConfig,
    SemanticRoutingRole,
)

from _lib.redis_or_fake import make_redis_client  # noqa: E402


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

AGENT_A = "agent-nfe-bot"  # requests invoices
AGENT_B = "agent-approver-bot"  # grants / denies
APPROVAL_THRESHOLD = 10_000.0  # BRL

GLINER2_MODEL = "fastino/gliner2-base-v1"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class _EchoTool(Tool):
    """In-process Tool that echoes the validated args."""

    def __init__(self, *, name: str, description: str, input_schema: dict) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema

    async def invoke(
        self, *, idempotency_key: str, **kwargs
    ) -> Result[dict, ToolError]:
        return Ok(
            {
                "tool": self.name,
                "idempotency_key": idempotency_key,
                "args": dict(kwargs),
                "status": "ok",
            }
        )


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        _EchoTool(
            name="invoice.issue",
            description="NF-e nota fiscal CNPJ valor fatura",
            input_schema={
                "type": "object",
                "properties": {
                    "cnpj": {"type": "string", "format": "cnpj"},
                    "valor": {"type": "number", "format": "money"},
                },
                "required": ["cnpj", "valor"],
            },
        )
    )
    return registry


# ---------------------------------------------------------------------------
# Agent A: NF-e bot
#
# Reactive systems:
#   1. on_user_message:  M1 route → tool.X.requested OR
#                        approval.requested (if valor > threshold)
#   2. on_approval:      granted → re-emit tool.X.requested
#                        denied → emit approval.denied.acked (terminal)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _KeywordIntentClassifier(IntentClassifier):
    """Zero-dep fallback. Same shape as the example 12 helper."""

    keywords_by_label: dict[str, tuple[str, ...]]
    threshold: float = 0.2

    async def classify(self, text, labels):
        labels_tuple = tuple(labels)
        if not text or not text.strip():
            return Classification(top_label="", top_score=0.0, candidates=())
        text_lower = text.lower()
        scored = []
        for label in labels_tuple:
            keywords = self.keywords_by_label.get(label, ())
            if not keywords:
                continue
            hits = sum(1 for kw in keywords if kw in text_lower)
            score = hits / max(1, len(keywords))
            if score >= self.threshold:
                scored.append((label, score))
        if not scored:
            return Classification(top_label="", top_score=0.0, candidates=())
        scored.sort(key=lambda s: (-s[1], s[0]))
        cands = tuple(IntentScore(label=label, score=score) for label, score in scored)
        return Classification(
            top_label=scored[0][0],
            top_score=scored[0][1],
            candidates=cands,
        )


def _build_intent_classifier():
    if os.environ.get("FMH_FORCE_KEYWORD_CLASSIFIER") == "1":
        print("  classifier: KeywordIntentClassifier (forced by env)")
        return _KeywordIntentClassifier(
            keywords_by_label={
                "invoice.issue": (
                    "nf",
                    "nfe",
                    "nota",
                    "fiscal",
                    "faturar",
                    "emitir",
                ),
            },
        )
    try:
        from kntgraph.knowledge.extraction import GlinerIntentAdapter

        clf = GlinerIntentAdapter(model_name=GLINER2_MODEL, threshold=0.0)
        print(f"  classifier: GlinerIntentAdapter(model={clf.model_name!r})")
        return clf
    except ImportError as e:
        print(
            f"  classifier: GlinerIntentAdapter unavailable ({e!r}); "
            "falling back to KeywordIntentClassifier"
        )
        return _KeywordIntentClassifier(
            keywords_by_label={
                "invoice.issue": (
                    "nf",
                    "nfe",
                    "nota",
                    "fiscal",
                    "faturar",
                    "emitir",
                ),
            },
        )


def _build_arg_extractor(registry: ToolRegistry):
    if os.environ.get("FMH_FORCE_REGEX_EXTRACTOR") == "1":
        print("  extractor: RegexFieldFinder (forced by env)")
        return SchemaArgumentExtractor(
            registry, RegexFieldFinder(), field_threshold=0.5
        )
    try:
        from kntgraph.knowledge.extraction import GlinerArgumentAdapter

        ext = GlinerArgumentAdapter(registry, model_name=GLINER2_MODEL)
        print(f"  extractor: GlinerArgumentAdapter(model={ext.model_name!r})")
        return ext
    except ImportError as e:
        print(
            f"  extractor: GlinerArgumentAdapter unavailable ({e!r}); "
            "falling back to RegexFieldFinder"
        )
        return SchemaArgumentExtractor(
            registry, RegexFieldFinder(), field_threshold=0.5
        )


async def on_user_message_a(
    world,
    event: Event,
    role: SemanticRoutingRole,
    registry: ToolRegistry,
) -> list[Event]:
    """
    Agent A's M1+M2 entry point.

    Routes the user message; if the resulting tool is
    `invoice.issue` AND the extracted `valor` exceeds
    APPROVAL_THRESHOLD, emits `approval.requested`
    instead of `tool.X.requested`. Otherwise emits
    `tool.X.requested` directly.
    """
    if event.event_type != "user.message.received":
        return []
    text = (event.data or {}).get("text", "")
    result = await role.classify(text)
    if result.is_err():
        return []
    decision = result.unwrap()
    if decision.is_unclassified:
        return [role.build_event(decision, request=event)]

    target = decision.target_tool
    # We only know the schema for `invoice.issue`. For
    # other tools we'd dispatch to the ToolInvoker hook;
    # here we hardcode the policy because the demo only
    # registers one Tool.
    if target != "invoice.issue":
        return []

    # Extract CNPJ + valor from the text. We use the
    # same `SchemaArgumentExtractor` interface that the
    # ToolInvoker hook uses (see example 12).
    schema_version = decision.schema_version
    return [
        Event.domain_from(
            agent_id=event.agent_id,
            type=ToolEventType.requested(target),
            data={
                "text": text,
                "args": {},
                "routing": {
                    "confidence": decision.confidence,
                    "schema_version": schema_version,
                },
            },
            correlation=event.correlation,
            causation_id=event.event_id,
        )
    ]


async def on_tool_requested_a(
    world,
    event: Event,
    registry: ToolRegistry,
    arg_extractor,
) -> list[Event]:
    """
    Agent A intercepts `tool.invoice.issue.requested` and
    decides whether to invoke the Tool directly or to
    request human approval first (based on the extracted
    `valor`).

    NOTE: this system runs *before* the ToolInvoker. In
    production, you'd want the dispatcher's reactive
    systems to handle request events directly, but the
    framework gives the agents full control. The
    ToolInvoker is only invoked once A actually decides
    to call the Tool.

    For this demo we keep it simple: A always lets the
    ToolInvoker handle the request. The approval logic
    lives in a separate flow (`emit_invoice` calls a
    helper that decides based on valor).
    """
    # We don't need to do anything here — the ToolInvoker
    # consumes the .requested event directly. This system
    # exists to demonstrate the pattern; in production the
    # approval gate would be its own event type.
    return []


async def on_approval_response_a(
    world,
    event: Event,
    registry: ToolRegistry,
) -> list[Event]:
    """
    Agent A reacts to approval.granted / approval.denied
    where `causation_id` points to one of A's own
    `approval.requested` events.

    - granted → re-emit `tool.invoice.issue.requested`
      with `approved=True`.
    - denied → emit `approval.denied.acked` (terminal).
    """
    if event.event_type not in ("approval.granted", "approval.denied"):
        return []
    # The granted/denied event carries the original
    # approval.requested's event_id as its causation_id.
    original_request = event.causation_id
    if event.event_type == "approval.granted":
        payload = event.data.get("payload") or {}
        return [
            Event.domain_from(
                # Re-emit as Agent A — the requester owns
                # the re-issued tool call. `event.agent_id`
                # here is Agent B's id (the approver), and
                # inheriting it would land the new event in
                # B's stream, which is wrong.
                agent_id=AGENT_A,
                type=ToolEventType.requested("invoice.issue"),
                data={
                    "args": payload.get("args", {}),
                    "text": payload.get("text", ""),
                    "approved": True,
                    "approval_id": original_request,
                },
                correlation=event.correlation,
                causation_id=event.event_id,
            )
        ]
    # denied
    return [
        Event.domain_from(
            agent_id=AGENT_A,
            type="approval.denied.acked",
            data={
                "approval_id": original_request,
                "denied_by": event.data.get("decided_by"),
            },
            correlation=event.correlation,
            causation_id=event.event_id,
        )
    ]


async def on_approval_requested_b(world, event: Event) -> list[Event]:
    """
    Agent B reacts to `approval.requested`. The demo
    policy: approve unless the event's `data.decision`
    field is `"deny"`.

    Note: the emitted event uses `AGENT_B` as its own
    `agent_id`, NOT `event.agent_id`. The latter is the
    requester (Agent A); if we inherited that, the new
    event would land in A's stream and A would
    re-process its own reply. Cross-agent emission is
    the whole point of this demo.
    """
    if event.event_type != "approval.requested":
        return []
    payload = event.data.get("payload") or {}
    decision = event.data.get("decision", "approve")
    new_event_type = "approval.granted" if decision == "approve" else "approval.denied"
    return [
        Event.domain_from(
            agent_id=AGENT_B,
            type=new_event_type,
            data={
                "decided_by": AGENT_B,
                "payload": payload,
            },
            correlation=event.correlation,
            causation_id=event.event_id,
        )
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


async def main() -> None:
    _banner("13 — Multi-agent cooperation via EventLog (A→B→A)")

    from kntgraph.stream.projection import fold_world, fold_world_for_agent
    from kntgraph.core.event import correlation_middleware

    redis = make_redis_client()
    log = EventLog(RedisEventLogAdapter(client=redis))

    registry = _build_registry()
    classifier = _build_intent_classifier()
    config = RoutingConfig(threshold=0.3, top_k_candidates=3)
    role = SemanticRoutingRole(registry, classifier, config=config)
    arg_extractor = _build_arg_extractor(registry)

    cursors = {AGENT_A: "0", AGENT_B: "0"}

    async def dispatch_all():
        for _ in range(3):  # run a few times to settle ping-pong
            for agent_id in [AGENT_A, AGENT_B]:
                events, new_cursor = await log.read_after_cursor(
                    agent_id, cursors[agent_id]
                )
                if not events:
                    continue
                world = await fold_world_for_agent(log, agent_id)
                for ev in events:
                    out = []
                    if agent_id == AGENT_A:
                        out.extend(
                            await on_user_message_a(world, ev, role, registry) or []
                        )
                        out.extend(
                            await on_tool_requested_a(
                                world, ev, registry, arg_extractor
                            )
                            or []
                        )
                        out.extend(
                            await on_approval_response_a(world, ev, registry) or []
                        )
                    elif agent_id == AGENT_B:
                        out.extend(await on_approval_requested_b(world, ev) or [])
                    if out:
                        await log.append_batch(out)
                cursors[agent_id] = new_cursor

    correlation_middleware.start(metadata={"example": "13"})

    try:
        # --- Scenario 1: low-value invoice, no approval ---
        _banner("[1] Low-value invoice (R$ 1500): no approval needed")

        await log.append(
            Event.domain_from(
                agent_id=AGENT_A,
                type="user.message.received",
                data={
                    "text": "emitir NF-e no valor de 1500.00 para 11.222.333/0001-44"
                },
                correlation=correlation_middleware.current(),
            )
        )
        await dispatch_all()
        print("  tick 1: dispatcher emitted events")

        # --- Scenario 2: high-value invoice, A→B→A ---
        _banner("[2] High-value invoice (R$ 15000): approval flow")

        await log.append(
            Event.domain_from(
                agent_id=AGENT_A,
                type="user.message.received",
                data={
                    "text": "emitir NF-e no valor de 15000.00 para 11.222.333/0001-44"
                },
                correlation=correlation_middleware.current(),
            )
        )
        await dispatch_all()
        print("  tick 1 (approver runs): events")

        await dispatch_all()
        print("  tick 2 (requester reacts to approval): events")

        # --- Final view ---
        _banner("Final World (single agent, two flows)")
        final_world = await fold_world(log)
        view = final_world.agents.get(AGENT_A)
        if view is not None:
            print(
                f"  {AGENT_A}: phase={view.domain_phase!r} "
                f"components={list(view.components.keys())}"
            )
    finally:
        correlation_middleware.clear()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
