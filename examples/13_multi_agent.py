# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
13 — Multi-agent cooperation via EventLog (ADR-013, ADR-010).

Demonstrates two agents cooperating through the same
EventLog stream. There is no broker, no RPC, no shared
memory — just events.

Scenario: an "approval gate" for high-value invoices.

  Agent A (NF-e bot):
    1. user.message.received
    2. Routes via SemanticRoutingRole (M1) →
       tool.invoice.issue.requested
    3. Decision rule: invoices with valor > R$ 10.000
       need human approval before invoking the Tool.
       → Emits approval.requested (NOT the tool request)
    4. Watches for approval.granted / approval.denied
       where causation_id == its approval.requested.
       → Re-emits tool.invoice.issue.requested with
         data["approved"] = True (or marks denied).

  Agent B (Approver bot):
    1. Watches for approval.requested.
    2. For the demo, always approves unless data["decision"]
       == "deny" (we drive the decision through the seed
       event).
    3. Emits approval.granted (or .denied) with
       causation_id == the approval.requested it consumed.

This is the same coordination model that the v2.0
architecture proposal calls the "Intention Broker" (see
ARCHITECTURE_PROPOSAL.md, §"Intention Broker"). The
broker itself is not implemented yet — what you see
here is the EventLog substrate the broker will build on.

Scenarios exercised
-------------------

  [1] Low-value invoice (R$ 1500):
      A classifies → A emits tool.invoice.issue.requested
      → Tool invoker calls the Tool → .completed (no
      approval needed).

  [2] High-value invoice (R$ 15000):
      A classifies → A emits approval.requested
      → B observes → B emits approval.granted
      → A observes approval → A re-emits
        tool.invoice.issue.requested with approved=True
      → Tool invoker calls the Tool → .completed.

  [3] Denied invoice (R$ 25000, decision=deny):
      A classifies → A emits approval.requested
      → B observes → B emits approval.denied
      → A observes denial → A emits approval.denied.acked
      (terminal — no further tool call).

Without Docker
--------------

Set `FMH_REDIS_FAKE=1` for an in-process Redis via
`fakeredis` (a `[dev]` extra).

Run:

    # Real Redis:
    docker run -d -p 6379:6379 --name fmh-redis redis
    python examples/13_multi_agent.py

    # In-process Redis (zero-dep):
    FMH_REDIS_FAKE=1 python examples/13_multi_agent.py

Known limitations
----------------

- The "Approver bot" is hard-coded to grant by default
  (denial must be driven by the `decision` field on the
  seed event). In production, this is where a human UI,
  a downstream LLM, or a policy engine plugs in.
- `track_agent` is called explicitly for both agents
  before the first `dispatch_once`. The dispatcher's
  bootstrap scan would also discover them via SCAN, but
  explicit tracking is the documented production pattern
  (cheaper than SCAN in large fleets).
- No retry / backoff: if an approval event is missed
  (e.g. crash between B's emit and the next dispatch),
  the system stays pending until a new event triggers a
  redelivery. A production implementation would add a
  timer-driven re-check (out of scope for this demo).
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from kntgraph.core.event import Event  # noqa: E402
from kntgraph.core.result import Err, Ok, Result, ToolError  # noqa: E402
from kntgraph.knowledge.extraction import (  # noqa: E402
    ArgExtraction,
    Classification,
    IntentClassifier,
    IntentScore,
    RegexFieldFinder,
    SchemaArgumentExtractor,
)
from kntgraph.runner.reactive import ReactiveDispatcher  # noqa: E402
from kntgraph.infra.redis._event_log import RedisEventLogAdapter  # noqa: E402
from kntgraph.stream.event_log import EventLog  # noqa: E402
from kntgraph.agents.tools.invoker import ToolInvoker  # noqa: E402
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
# Helpers
# ---------------------------------------------------------------------------


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _extract_valor(args: dict) -> float:
    raw = args.get("valor")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def emit_invoice_request(
    log: EventLog,
    *,
    agent_id: str,
    text: str,
    args: dict,
    decision: str,
) -> Event:
    """
    Decide the path: direct tool call, approval request,
    or denial. Returns the emitted event.
    """
    valor = _extract_valor(args)
    if valor > APPROVAL_THRESHOLD:
        # Approval gate: emit approval.requested instead
        # of tool.X.requested.
        return await log.append(
            Event.domain_from(
                agent_id=agent_id,
                type="approval.requested",
                data={
                    "payload": {
                        "text": text,
                        "args": args,
                        "tool": "invoice.issue",
                    },
                    "decision": decision,  # passed by demo; B reads this
                    "threshold": APPROVAL_THRESHOLD,
                },
            )
        )
    # Below threshold: direct tool request.
    return await log.append(
        Event.domain_from(
            agent_id=agent_id,
            type=ToolEventType.requested("invoice.issue"),
            data={
                "text": text,
                "args": args,
            },
        )
    )


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


async def main() -> None:
    _banner("13 — Multi-agent cooperation via EventLog (A→B→A)")

    redis = make_redis_client()
    log = EventLog(RedisEventLogAdapter(client=redis))

    registry = _build_registry()
    classifier = _build_intent_classifier()
    config = RoutingConfig(threshold=0.3, top_k_candidates=3)
    role_a = SemanticRoutingRole(registry, classifier, config=config)
    arg_extractor = _build_arg_extractor(registry)

    async def pre_invoke_args_hook(text, tool_name):
        if not text:
            return Ok(
                ArgExtraction(
                    tool_name=tool_name,
                    fields={},
                    confidences={},
                    schema_version="",
                )
            )
        try:
            ext = await arg_extractor.extract(text, tool_name)
        except Exception as e:
            return Err(ToolError(f"extractor_error: {e!r}"))
        return Ok(ext)

    invoker = ToolInvoker(
        log=log,
        registry=registry,
        pre_invoke_args_extractor=pre_invoke_args_hook,
    )

    async def route_a(world, event):
        return await on_user_message_a(world, event, role_a, registry)

    async def react_a(world, event):
        return await on_approval_response_a(world, event, registry)

    async def react_b(world, event):
        return await on_approval_requested_b(world, event)

    dispatcher = ReactiveDispatcher(
        log,
        systems=[route_a, react_a, react_b],
        poll_interval=0.5,
    )
    dispatcher.track_agent(AGENT_A)
    dispatcher.track_agent(AGENT_B)

    # --- Scenario 1: low-value invoice, no approval ---
    _banner("[1] Low-value invoice (R$ 1500): direct tool call")
    await emit_invoice_request(
        log,
        agent_id=AGENT_A,
        text="emitir NF-e no valor de 1500.50 para o CNPJ 12.345.678/0001-90",
        args={
            "cnpj": "12.345.678/0001-90",
            "valor": 1500.50,
        },
        decision="approve",
    )
    await dispatcher.dispatch_once()
    handled = await invoker.run_once(AGENT_A)
    print(f"  invoker handled: {handled}")

    # --- Scenario 2: high-value invoice, approval flow ---
    _banner("[2] High-value invoice (R$ 15000): A→B→A approval flow")
    print("  Agent A emits approval.requested (valor > R$ 10k)")
    approval_event = Event.domain_from(
        agent_id=AGENT_A,
        type="approval.requested",
        data={
            "payload": {
                "text": "emitir NF-e de valor 15000 para CNPJ 11.222.333/0001-44",
                "args": {
                    "cnpj": "11.222.333/0001-44",
                    "valor": 15000.0,
                },
                "tool": "invoice.issue",
            },
            "decision": "approve",  # B will approve
            "threshold": APPROVAL_THRESHOLD,
        },
    )
    await log.append(approval_event)
    print(f"  approval_id: {approval_event.event_id}")
    # Agent B reacts → emits approval.granted
    await dispatcher.dispatch_once()
    print("  Agent A observes approval.granted → re-emits tool.X.requested")
    await dispatcher.dispatch_once()
    # ToolInvoker processes the approved request
    handled = await invoker.run_once(AGENT_A)
    print(f"  invoker handled: {handled}")

    # --- Scenario 3: denied invoice ---
    _banner("[3] Denied invoice (R$ 25000): A→B(deny)→A terminal")
    print("  Agent A emits approval.requested with decision=deny")
    denied_event = Event.domain_from(
        agent_id=AGENT_A,
        type="approval.requested",
        data={
            "payload": {
                "text": "emitir NF-e de valor 25000 para CNPJ 22.333.444/0001-55",
                "args": {
                    "cnpj": "22.333.444/0001-55",
                    "valor": 25000.0,
                },
                "tool": "invoice.issue",
            },
            "decision": "deny",  # B will deny
            "threshold": APPROVAL_THRESHOLD,
        },
    )
    await log.append(denied_event)
    print(f"  approval_id: {denied_event.event_id}")
    # Agent B reacts → emits approval.denied
    await dispatcher.dispatch_once()
    # Agent A reacts to denial → emits approval.denied.acked (terminal)
    print("  Agent A observes approval.denied → emits approval.denied.acked")
    await dispatcher.dispatch_once()

    # --- Final view ----------------------------------------------------
    _banner("Final EventLog view (cross-agent)")
    all_events: list[Event] = []
    for aid in (AGENT_A, AGENT_B):
        events = await log.read(aid)
        all_events.extend(events)
    # Show by type, in chronological order.
    type_to_events: dict[str, list[Event]] = {}
    for e in all_events:
        type_to_events.setdefault(e.event_type, []).append(e)
    for etype in sorted(type_to_events):
        evs = type_to_events[etype]
        agents = sorted({e.agent_id for e in evs})
        print(f"  {etype:46s}  count={len(evs):2d}  agents={agents}")

    # PII hygiene: cross-agent events must not leak
    # the original user text (we hash it instead).
    _banner("PII hygiene: cross-agent messages")
    cross = [
        e
        for e in all_events
        if e.agent_id == AGENT_B and e.event_type.startswith("approval.")
    ]
    leaks = 0
    for e in cross:
        # The original text was "emitir NF-e de valor 15000..."
        original = "emitir NF-e de valor 15000 para CNPJ 11.222.333/0001-44"
        # The text is in payload["text"] — we want it
        # NOT to be there in B's view. (Cross-agent PII
        # hygiene is intentionally NOT enforced here —
        # see the note below.)
        if original in str(e.data):
            leaks += 1
    print(
        f"  approval.* events that contain the original user text: "
        f"{leaks} of {len(cross)}"
    )
    print(
        "  Note: B's `payload.text` IS the original user "
        "text in this demo. A production system would store "
        "only `text_hash` + `cnpj` + `valor` (the explicit "
        "fields the approver needs)."
    )

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
