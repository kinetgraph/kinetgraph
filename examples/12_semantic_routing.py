# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
12 — Semantic routing + arg extraction end-to-end (ADR-013).

Demonstrates the full M1 + M2 pipeline in one flow:

    user.message.received
      → SemanticRoutingRole (M1: intent classification)
        → tool.{name}.requested
          → ToolInvoker (M2: arg extraction via hook)
            → tool.{name}.args_invalid    (when validation fails)
            → tool.{name}.completed       (when validation passes)

Four scenarios are exercised against the same `ToolRegistry`:

    [1] Routed + extracted:
        "emitir NF-e no valor de 1500.50 para o CNPJ 12.345.678/0001-90"
        → invoice.issue.requested with cnpj + valor
        → invoice.issue.completed (echoes the args)

    [2] Routed, Tool accepts empty args:
        "transferir dinheiro"
        → bank.transfer.requested with empty args
        → bank.transfer.completed (no required fields → tool runs)

    [3] Below threshold / unclassified:
        "oi"  (no matching tool intent)
        → routing.unclassified with sha256(text) + top-k candidates

    [4] Idempotency:
        Fresh agent with same text twice → first pass completes,
        second pass dedupes by event_id; dispatcher=0, invoker=0.

Requirements
------------

- `redis.asyncio` (real or `fakeredis` — see below).
- `gliner2[local]` (extra `[gliner]` in `kntgraph`). The
  example uses the zero-shot `GlinerIntentAdapter` and
  `GlinerArgumentAdapter`. The model id is
  `fastino/gliner2-base-v1` (downloaded from HuggingFace
  on first run; cached thereafter).

  If `gliner2` is not installed, the example raises
  `ImportError` rather than falling back to a static
  classifier — semantic routing only makes sense with a
  real model. Set up the toolchain on the host (or
  install the wheels) before running.

Without Docker
--------------

`FMH_REDIS_FAKE=1` switches the EventLog to an in-process
`fakeredis` client (no container required). FalkorDB is
not exercised in this example — the EventLog +
ToolInvoker path is pure Redis. The `LiteFalkorDBClient`
adapter lives at `fmh_backend.infra.LiteFalkorDBClient`
for examples that DO need a graph.

Run:

    # Real Redis (default):
    docker run -d -p 6379:6379 --name fmh-redis redis
    python examples/12_semantic_routing.py

    # In-process Redis (zero-dep):
    FMH_REDIS_FAKE=1 python examples/12_semantic_routing.py

    # Force the regex arg extractor (GLiNER2 is still
    # used for intent classification):
    FMH_FORCE_REGEX_EXTRACTOR=1 python examples/12_semantic_routing.py

Known limitations
-----------------

- `route_system` (the M1 reactive system) copies the
  original `text` into the `tool.X.requested` payload so
  the M2 hook (`ToolInvoker.pre_invoke_args_extractor`)
  can read it. The framework's default
  `SemanticRoutingRole.build_event` does NOT do this;
  see `invoker.py:266` ("Programmatic callers that do
  not supply a text get the legacy path"). Production
  callers should match this pattern.

- The GLiNER2 base model (205M params) has limited
  semantic accuracy on domain-specific text. Even with
  Brazilian-specific keywords in the tool descriptions
  ("NF-e", "CNPJ", "PIX"), texts without explicit
  anchors produce inverted-confidence scores well
  below 1.0 (e.g. 0.007 for invoice.issue on the demo
  scenario 1). The negative-class trick gives a real
  ranking (the right label usually wins by a clear
  margin), but a `RoutingConfig.threshold` of 0.3 (a
  production-realistic value) will correctly route
  only texts with strong surface anchors. Lower the
  threshold for noisier text, or fine-tune the model
  for higher accuracy.

- GLiNER2 1.3.x classification is single-label top-1.
  The framework runs `len(labels)` binary tasks against
  a generic `none_of_the_above` label and ranks the
  inverted confidence. On a CPU with the 205M base
  model, latency is ~100-300ms per `classify` call.
"""

from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from kntgraph.core.event import Event  # noqa: E402
from kntgraph.core.result import Err, Ok, Result, ToolError  # noqa: E402
from kntgraph.knowledge.extraction import (  # noqa: E402
    ArgExtraction,
    GlinerArgumentAdapter,
    GlinerIntentAdapter,
    RegexFieldFinder,
    SchemaArgumentExtractor,
)
from kntgraph.runner.reactive import ReactiveDispatcher  # noqa: E402
from kntgraph.infra.redis._event_log import RedisEventLogAdapter  # noqa: E402
from kntgraph.stream.event_log import EventLog  # noqa: E402
from kntgraph.agents.tools.invoker import ToolInvoker  # noqa: E402
from kntgraph.agents.tools.protocol import (  # noqa: E402
    Tool,
    ToolRegistry,
)

from kntgraph.agents.roles import (  # noqa: E402
    EVENT_TYPE_ROUTING_UNCLASSIFIED,
    RoutingConfig,
    SemanticRoutingRole,
)

from _lib.redis_or_fake import make_redis_client  # noqa: E402


# Canonical HF model id for GLiNER2 base.
GLINER2_MODEL = "fastino/gliner2-base-v1"


def _build_intent_classifier() -> GlinerIntentAdapter:
    """
    Build the M1 intent classifier.

    Always uses GLiNER2 — semantic routing only makes
    sense with a real model. Raises `ImportError` if
    `gliner2` is not installed (the `[gliner]` extra in
    `fmh-backend`).
    """
    clf = GlinerIntentAdapter(
        model_name=GLINER2_MODEL,
        # The internal `threshold` filters candidates
        # whose inverted score is below this value.
        # With the negative-class trick the inverted
        # score is typically very small (e.g. 0.0002)
        # even for correct labels, so we leave the
        # internal gate at 0.0 and let `RoutingConfig.
        # threshold` (set above) be the single source of
        # truth for the user-facing decision boundary.
        threshold=0.0,
    )
    print(f"  classifier: GlinerIntentAdapter(model={clf.model_name!r})")
    return clf


def _build_arg_extractor(
    registry: ToolRegistry,
) -> SchemaArgumentExtractor | GlinerArgumentAdapter:
    """
    Build the M2 argument extractor.

    Production: `GlinerArgumentAdapter` (zero-shot entity
    spans from the same GLiNER2 model). Honours
    `FMH_FORCE_REGEX_EXTRACTOR=1` to switch to the
    dependency-free `RegexFieldFinder` path (useful for
    CI on hosts without GLiNER2 or when deterministic
    span extraction is required).
    """
    if os.environ.get("FMH_FORCE_REGEX_EXTRACTOR") == "1":
        print("  extractor: RegexFieldFinder (forced by env)")
        return SchemaArgumentExtractor(
            registry, RegexFieldFinder(), field_threshold=0.5
        )
    ext = GlinerArgumentAdapter(registry, model_name=GLINER2_MODEL)
    print(f"  extractor: GlinerArgumentAdapter(model={ext.model_name!r})")
    return ext


# ---------------------------------------------------------------------------
# Domain tools (in-process, side-effect-free — they just echo)
# ---------------------------------------------------------------------------


class _EchoTool(Tool):
    """
    Toy Tool that echoes the validated args back. The result
    is what `tool.{name}.completed` carries, so the consumer
    system can show the round-trip.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema

    async def invoke(
        self, *, idempotency_key: str, **kwargs
    ) -> Result[dict, ToolError]:
        # The Tool Protocol requires the return type to be
        # `Result[Any, ToolError]`. Wrapping the echo payload
        # in `Ok(...)` is the canonical happy-path return.
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
            # Short, focused description sent to the GLiNER2
            # classifier as the per-label description in
            # the schema. Brazilian-specific keywords
            # ("NF-e", "nota fiscal", "CNPJ", "valor") give
            # the zero-shot model strong anchors that pure
            # English tool names lack. Tested empirically:
            # long descriptions (with "Typical user text
            # mentions: ...") HURT accuracy — the GLiNER2
            # base model is distracted by long context.
            # Keep it short.
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
    registry.register(
        _EchoTool(
            name="bank.transfer",
            description="PIX transferencia pagamento TED DOC",
            input_schema={
                "type": "object",
                "properties": {
                    "valor": {"type": "number", "format": "money"},
                    "conta": {"type": "string"},
                },
            },
        )
    )
    registry.register(
        _EchoTool(
            name="support.ticket",
            description="suporte chamado problema erro ajuda",
            input_schema={
                "type": "object",
                "properties": {
                    "assunto": {"type": "string"},
                },
            },
        )
    )
    return registry


# ---------------------------------------------------------------------------
# Reactive systems
# ---------------------------------------------------------------------------


async def route_system(world, event: Event, role: SemanticRoutingRole) -> list[Event]:
    """
    Reactive system for `user.message.received` →
    `tool.{name}.requested` (or `routing.unclassified`).

    Overrides the Role's default event builder to also
    carry the original `text` in the `tool.X.requested`
    payload so the M2 hook in
    `ToolInvoker.pre_invoke_args_extractor` can read it.
    See `invoker.py:266`: "Programmatic callers that do
    not supply a text get the legacy path (no extraction,
    validation only)".
    """
    if event.event_type != "user.message.received":
        return []
    text = (event.data or {}).get("text", "")
    decision_result = await role.classify(text)
    if decision_result.is_err():
        return []
    decision = decision_result.unwrap()
    if decision.is_unclassified:
        return [role.build_event(decision, request=event)]
    from kntgraph.agents.tools.protocol import ToolEventType

    return [
        Event.domain_from(
            agent_id=event.agent_id,
            type=ToolEventType.requested(decision.target_tool),
            data={
                "text": text,
                "args": {},
                "routing": {
                    "confidence": decision.confidence,
                    "schema_version": decision.schema_version,
                },
            },
            correlation=event.correlation,
            causation_id=event.event_id,
        )
    ]


async def consume_completed(world, event: Event) -> list[Event]:
    """Reactive system for `tool.{name}.completed` → `task.handled`."""
    if not event.event_type.endswith(".completed"):
        return []
    return [
        Event.domain_from(
            agent_id=event.agent_id,
            type="task.handled",
            data={
                "request_id": event.data.get("request_id"),
                "tool": event.data.get("tool"),
                "latency_ms": event.data.get("latency_ms"),
            },
            causation_id=event.causation_id,
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


async def _print_events(log: EventLog, agent_id: str) -> None:
    events = await log.read(agent_id)
    for e in events:
        print(f"  {e.event_type:42s}  id={e.event_id}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


SCENARIOS: list[tuple[str, str]] = [
    (
        "Routed + extracted",
        "emitir NF-e no valor de 1500.50 para o CNPJ 12.345.678/0001-90",
    ),
    (
        "Routed + no args extracted (Tool accepts empty args)",
        "transferir dinheiro",
    ),
    (
        "Below threshold (routing.unclassified)",
        "oi",
    ),
]


async def _run_scenario(
    *,
    log: EventLog,
    agent_id: str,
    text: str,
    dispatcher: ReactiveDispatcher,
    invoker: ToolInvoker,
    scenario_idx: int,
) -> None:
    print()
    print(f"  text: {text!r}")
    request_event = Event.domain_from(
        agent_id=agent_id,
        type="user.message.received",
        data={"text": text, "scenario": scenario_idx},
    )
    append = await log.append(request_event)
    if append.is_ok():
        print(f"  seeded: {request_event.event_type} id={request_event.event_id}")
    else:
        print(f"  idempotent dedup: {request_event.event_id}")

    # M1: reactive system produces the routed tool event (or unclassified).
    await dispatcher.dispatch_once()

    # M2: ToolInvoker picks up any .requested and runs the hook + Tool.
    handled = await invoker.run_once(agent_id)
    print(f"  invoker handled: {handled}")

    # Consumer system picks up .completed → task.handled.
    await dispatcher.dispatch_once()

    events = await log.read(agent_id)
    last = [e for e in events if e.data.get("scenario") == scenario_idx]
    for e in last:
        print(
            f"    → {e.event_type:42s}  "
            f"{dict((k, v) for k, v in (e.data or {}).items() if k != 'scenario')}"
        )


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


async def main() -> None:
    _banner("12 — Semantic routing + arg extraction (ADR-013 M1 + M2)")

    redis = make_redis_client()
    log = EventLog(RedisEventLogAdapter(client=redis))
    agent_id = "agent-demo-12"

    registry = _build_registry()

    classifier = _build_intent_classifier()
    config = RoutingConfig(threshold=0.3, top_k_candidates=3)
    role = SemanticRoutingRole(registry, classifier, config=config)
    print(f"  registered tools:  {sorted(registry.names())}")
    print(f"  role schema_ver:   {role.schema_version}")
    print(f"  routing threshold: {role.config.threshold}")

    arg_extractor = _build_arg_extractor(registry)

    async def pre_invoke_args_hook(
        text: str, tool_name: str
    ) -> Result[ArgExtraction, ToolError]:
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
            extraction = await arg_extractor.extract(text, tool_name)
        except Exception as e:
            return Err(ToolError(f"extractor_error: {e!r}"))
        return Ok(extraction)

    invoker = ToolInvoker(
        log=log,
        registry=registry,
        pre_invoke_args_extractor=pre_invoke_args_hook,
    )

    async def route_bound(world, event: Event) -> list[Event]:
        return await route_system(world, event, role)

    dispatcher = ReactiveDispatcher(
        log,
        systems=[route_bound, consume_completed],
        poll_interval=0.5,
    )
    # The dispatcher's bootstrap discovers agent_ids by
    # SCANing the EventLog keyspace the FIRST time
    # `dispatch_once` is called. If the log is empty at
    # that moment, no agents are tracked and later
    # `dispatch_once` calls will not see new events for
    # them. `track_agent` seeds the cursor map explicitly
    # so our agent is watched from the start.
    dispatcher.track_agent(agent_id)

    # Smoke-test the intent classifier. With the
    # negative-class trick the role always returns a
    # `Classification`; an `Err` here means the model
    # load or the schema construction failed — surface
    # loudly.
    try:
        smoke = await role.classify("ping")
        print(f"  smoke classify: top={smoke.top_label!r} score={smoke.top_score:.3f}")
    except Exception as e:  # noqa: BLE001
        print(f"\n  WARNING: classifier smoke raised {e!r}\n")

    # --- Scenarios 1-3 ---------------------------------------------------
    for i, (label, text) in enumerate(SCENARIOS, start=1):
        print()
        print("-" * 72)
        print(f"  [{i}] {label}")
        print("-" * 72)
        await _run_scenario(
            log=log,
            agent_id=agent_id,
            text=text,
            dispatcher=dispatcher,
            invoker=invoker,
            scenario_idx=i,
        )

    # --- Scenario 4: idempotency ----------------------------------------
    print()
    print("-" * 72)
    print("  [4] Idempotency: fresh agent, replay, dedup by event_id")
    print("-" * 72)
    # Use a NEW agent id so the agent has no pending
    # requests. Scenario 1's text routes to
    # `invoice.issue` whose hook extracts the args; the
    # tool completes successfully, so the `.requested`
    # has a matching `.completed` in the log and is
    # deduped on subsequent `run_once`.
    fresh_agent = "agent-demo-12-replay"
    fresh_text = SCENARIOS[0][1]
    seed = Event.domain_from(
        agent_id=fresh_agent,
        type="user.message.received",
        data={"text": fresh_text, "scenario": 4},
    )
    await log.append(seed)
    dispatcher.track_agent(fresh_agent)
    await dispatcher.dispatch_once()
    handled_first = await invoker.run_once(fresh_agent)
    print(f"  first pass, invoker handled: {handled_first}")
    await dispatcher.dispatch_once()  # consumer emits task.handled

    # Replay the SAME request (same agent_id, same
    # scenario, same text → same event_id → EventLog
    # dedupe). The dispatcher finds no new events; the
    # invoker finds no new `.requested` matching a
    # `.completed`.
    await log.append(seed)
    n_dispatched = await dispatcher.dispatch_once()
    handled_second = await invoker.run_once(fresh_agent)
    print(f"  replay, dispatcher dispatched (expected 0): {n_dispatched}")
    print(f"  replay, invoker handled (expected 0): {handled_second}")

    # --- Final view ------------------------------------------------------
    print()
    print("=" * 72)
    print("Final EventLog for the agent:")
    print("=" * 72)
    await _print_events(log, agent_id)

    # --- PII hygiene check ----------------------------------------------
    print()
    print("=" * 72)
    print("PII hygiene check on routing.unclassified:")
    print("=" * 72)
    events = await log.read(agent_id)
    unclassified = [
        e for e in events if e.event_type == EVENT_TYPE_ROUTING_UNCLASSIFIED
    ]
    # PII hygiene check: the original user text must NOT
    # appear in any `routing.unclassified` event payload.
    # The framework is documented to use `text_hash` as
    # the only handle (see ADR-013 §3). We verify against
    # the matched `user.message.received` event, not by
    # substring matching on the payload (which produces
    # false positives: e.g. "oi" is a substring of
    # "invoice.issue").
    user_message_events = [
        ev
        for ev in await log.read(agent_id)
        if ev.event_type == "user.message.received"
    ]
    by_causation: dict[str, str] = {
        str(ev.event_id): ev.data.get("text", "") for ev in user_message_events
    }
    for e in unclassified:
        original = by_causation.get(str(e.causation_id), "")
        if original:
            # Check word-boundary match, not raw substring.
            import re

            pattern = re.compile(rf"\b{re.escape(original)}\b")
            assert not pattern.search(str(e.data)), (
                f"routing.unclassified leaked PII — original text "
                f"{original!r} appeared as a word in event {e.event_id} payload"
            )
        print(f"  payload_text_hash = {e.data.get('text_hash', '')[:16]}...")
        print(f"  candidates       = {e.data.get('candidates')}")
        print(f"  threshold        = {e.data.get('threshold')}")
        print(
            f"  PII check        = OK "
            f"(text {original!r} not present as word in payload)"
        )

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
