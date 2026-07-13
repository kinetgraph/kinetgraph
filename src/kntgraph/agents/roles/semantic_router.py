# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
SemanticRoutingRole — turn `user.message.received` into a
`tool.{name}.requested` event using a zero-shot intent
classifier (ADR-013, Momento 1).

Why a Role and not a Tool
-------------------------

A Role is "semantic specialisation". It knows the prompt /
schema of its domain, sits in front of a capability (here,
an `IntentClassifier`) injected via constructor, and
returns a typed result. It is a class Python — NOT a
`Tool`, NOT registered in the `ToolRegistry` (ADR-006).

For the routing case, the Role's job is:

  1. Receive an inbound `user.message.received` event
     (the `IntentRouter` HTTP gateway is the typical
     source; the Role is event-source-agnostic).
  2. Call the `IntentClassifier` with the user text and
     the set of tool names from the `ToolRegistry`
     (computed at construction time).
  3. Decide between the two outcomes:
       a. `confidence >= threshold` →
          emit `tool.{target_tool}.requested` with
          `args={}` (Momento 2 will fill it later).
       b. `confidence < threshold` (or no decision) →
          emit `routing.unclassified` for the DLQ / LLM
          consumer (ADR-013 §2.1).
  4. Return a typed `RoutingDecision` for the caller
     (the reactive system or test).

The actual `EventLog.append` is left to the caller
(reactive system or test). The Role does not know about
Redis Streams — keeping it framework-pure, easy to test.

Determinism
-----------

`event_id` for the emitted event is `uuid5(NAMESPACE,
sha256(text + schema_version + target_tool))` — same
input → same event id → the EventLog dedupes replays
(consistent with `api/intent_router.py:_deterministic_event_id`).
The role does NOT persist; it just computes the id and
lets the caller append.

Schema versioning
-----------------

`schema_version` is a hash of the sorted tool names. If
the registry changes, the version changes, and any
cached classifications are invalidated. Roles that
cache per (text, schema_version) get this for free.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from kntgraph.core.event import (
    Event,
)
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core._typing import JsonValue
from kntgraph.infra.hashing import short_hash
from kntgraph.knowledge.extraction import (
    Classification,
    IntentClassifier,
)
from kntgraph.agents.tools.protocol import ToolEventType, ToolRegistry
from kntgraph.infra.config import BaseSettings

# Namespace reserved for routing-event ids. Kept in this
# module (not `core/event.py`) so the framework stays
# agnostic of the routing use case.
_ROUTING_EVENT_NAMESPACE = UUID("5d3f1a70-9b8a-4c1e-8d4f-3a9b7c2e1f50")

EVENT_TYPE_USER_MESSAGE = "user.message.received"
EVENT_TYPE_ROUTING_UNCLASSIFIED = "routing.unclassified"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class _RoutingSettings(BaseSettings):
    """
    Internal Pydantic-settings wrapper for `RoutingConfig`.
    Honours `KNT_ROUTING_*` env vars with type coercion.
    """

    model_config = BaseSettings.model_config | {
        "env_prefix": "KNT_ROUTING_",
    }

    threshold: float = 0.6
    top_k_candidates: int = 3


@dataclass(frozen=True)
class RoutingConfig:
    """
    Tunables for the semantic router.

    `threshold` is the minimum top-1 score for the role to
    commit to a `tool.{name}.requested`. Below it, the
    role emits `routing.unclassified` instead. `0.6` is
    the default per ADR-013; tune per deployment.

    `top_k_candidates` is how many top candidates are
    included in the `routing.unclassified` event payload
    for audit / fallback. 3 is a reasonable default —
    enough to disambiguate, small enough to keep the
    event compact.

    `request_event_type` lets the same role listen to a
    different inbound event name (e.g. an application
    that has its own `chat.user_message` event). Default
    matches the framework's HTTP gateway.
    """

    threshold: float = 0.6
    top_k_candidates: int = 3
    request_event_type: str = EVENT_TYPE_USER_MESSAGE
    env_prefix: str = "KNT_ROUTING_"

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1], got {self.threshold!r}")
        if self.top_k_candidates <= 0:
            raise ValueError(
                f"top_k_candidates must be > 0, got {self.top_k_candidates!r}"
            )

    @classmethod
    def from_env(cls, prefix: str = "KNT_ROUTING_") -> "RoutingConfig":
        """
        Build a `RoutingConfig` from env vars.

        Reads (via `_RoutingSettings`, a Pydantic
        `BaseSettings`):
          - `KNT_ROUTING_THRESHOLD` (float, default 0.6)
          - `KNT_ROUTING_TOP_K_CANDIDATES` (int, default 3)

        Malformed values now raise a clear validation
        error instead of silently falling back to the
        default — silently masking a typo
        (`KNT_ROUTING_THRESHOLD=0,6` written in pt-BR)
        was the previous behaviour and the new contract
        is louder. To preserve compatibility with callers
        passing a non-default `prefix`, that argument is
        accepted but only the canonical `KNT_ROUTING_`
        is honoured by the underlying settings class.

        Variables already set in the process win over the
        `.env` file (the same `override=False` semantics
        the rest of the workspace uses).
        """
        if prefix != "KNT_ROUTING_":
            import warnings

            warnings.warn(
                f"RoutingConfig.from_env honours only the "
                f"KNT_ROUTING_ prefix; requested "
                f"{prefix!r} is ignored.",
                UserWarning,
                stacklevel=2,
            )
        env = _RoutingSettings()
        return cls(
            threshold=env.threshold,
            top_k_candidates=env.top_k_candidates,
            env_prefix=prefix,
        )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingDecision:
    """
    The Role's decision for one inbound message.

    `target_tool` is empty when `is_unclassified` is True
    (the route could not be determined with sufficient
    confidence). `confidence` is always set (0.0 for the
    unclassified case) so downstream code can branch on
    the float without re-parsing strings.

    `candidates` is the top-`top_k_candidates` from the
    classifier, in descending order. Kept for audit and
    for the LLM fallback consumer that wants to know
    "the model was torn between A and B".

    `schema_version` is the hash of the sorted tool
    names. Useful for cache invalidation and for
    understanding why the same input was routed
    differently after a registry change.
    """

    target_tool: str
    confidence: float
    candidates: tuple[tuple[str, float], ...]
    schema_version: str
    is_unclassified: bool = False


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------


class SemanticRoutingRole:
    """
    Classify a user message into a tool and emit the
    corresponding event.

    Construction takes:
      - `registry`: the `ToolRegistry` whose tools are
        candidate targets. The set of `tool.name` is
        snapshotted at construction; rebuild the role
        when the registry changes.
      - `classifier`: any `IntentClassifier`. Production
        uses `GlinerIntentAdapter`; tests use a fake.
      - `config`: tunables. Defaults are sensible.

    The Role is **stateless** across calls: each
    `classify(text)` is independent. The Role is safe
    to share across agents / coroutines (the classifier
    is the only shared resource and it is pure).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        classifier: IntentClassifier,
        *,
        config: Optional[RoutingConfig] = None,
    ) -> None:
        if registry is None:
            raise ValueError("registry is required")
        if classifier is None:
            raise ValueError("classifier is required")
        self._registry = registry
        self._classifier = classifier
        self._config = config or RoutingConfig()
        # Snapshot the candidate labels at construction
        # (ADR-013 §2.1: recalcular a cada instanciação).
        self._labels: tuple[str, ...] = tuple(registry.names())
        # Snapshot per-tool descriptions in the SAME order
        # as the labels. Used to give the zero-shot
        # classifier domain-specific keywords (e.g.
        # "NF-e", "CNPJ", "PIX") that pure English tool
        # names lack. Tools without a description get an
        # empty string (the classifier tolerates that).
        self._descriptions: tuple[str, ...] = tuple(
            (registry.get(name).description if registry.get(name) else "")
            for name in self._labels
        )
        self._schema_version = self._compute_schema_version(self._labels)
        if not self._labels:
            # We do not raise here: an empty registry is a
            # legitimate startup state, and the caller may
            # intend to populate it later. We DO log via
            # structlog at first classify call.
            pass

    # ------------------------------------------------------------------ accessors

    @property
    def labels(self) -> tuple[str, ...]:
        """The snapshot of tool names used as candidate labels."""
        return self._labels

    @property
    def schema_version(self) -> str:
        """Hash of the sorted label set. Cache key suffix."""
        return self._schema_version

    @property
    def config(self) -> RoutingConfig:
        return self._config

    # ------------------------------------------------------------------ classify

    async def classify(self, text: str) -> Result[RoutingDecision, ToolError]:
        """
        Classify `text` into a tool decision.

        Returns `Ok(RoutingDecision(...))` for both the
        "routed" and the "unclassified" outcomes — the
        caller decides what to do based on
        `decision.is_unclassified`. Returns `Err(...)` only
        for hard failures (empty input, classifier
        exception that the role caught).
        """
        if not text or not text.strip():
            return Err(ToolError("empty text"))

        if not self._labels:
            # No tools registered → every message is
            # unclassified. Treat as a successful no-op.
            decision = RoutingDecision(
                target_tool="",
                confidence=0.0,
                candidates=(),
                schema_version=self._schema_version,
                is_unclassified=True,
            )
            return Ok(decision)

        try:
            # Pass descriptions only when at least one
            # tool actually has a description; otherwise
            # stay on the bare-labels path.
            descriptions = self._descriptions if any(self._descriptions) else None
            classification = await self._classifier.classify(  # type: ignore[call-arg]
                text, self._labels, descriptions=descriptions
            )
        except Exception as e:  # noqa: BLE001 — role boundary
            return Err(ToolError(f"classifier_error: {e!r}"))

        decision = self._decide(classification)
        return Ok(decision)

    # ------------------------------------------------------------------ event

    def build_event(
        self,
        decision: RoutingDecision,
        *,
        request: Event,
    ) -> Event:
        """
        Build the `tool.{name}.requested` (or
        `routing.unclassified`) event for a `decision`.

        `request` is the inbound `user.message.received`
        event. The new event uses it as
        `causation_id` and inherits the `correlation_id`
        from its `CorrelationContext` (per ADR-013 §2.1).

        The `event_id` is deterministic: same
        (request_event_id, decision.target_tool,
        decision.schema_version) → same event_id, so
        replays dedup at the EventLog level.
        """
        if decision.is_unclassified:
            return self._build_unclassified_event(decision, request)

        tool_name = decision.target_tool
        payload = {
            "args": {},
            "routing": {
                "confidence": decision.confidence,
                "schema_version": decision.schema_version,
            },
        }
        return Event.domain_from(
            agent_id=request.agent_id,
            type=ToolEventType.requested(tool_name),
            data=payload,
            correlation=request.correlation,
            causation_id=request.event_id,
        )

    # ------------------------------------------------------------------ internals

    def _decide(self, classification: Classification) -> RoutingDecision:
        """
        Apply the threshold and shape the output.
        """
        top_label = classification.top_label
        top_score = classification.top_score
        candidates = tuple((c.label, c.score) for c in classification.candidates)[
            : self._config.top_k_candidates
        ]

        if not top_label or top_score < self._config.threshold:
            return RoutingDecision(
                target_tool="",
                confidence=top_score,
                candidates=candidates,
                schema_version=self._schema_version,
                is_unclassified=True,
            )

        return RoutingDecision(
            target_tool=top_label,
            confidence=top_score,
            candidates=candidates,
            schema_version=self._schema_version,
            is_unclassified=False,
        )

    def _build_unclassified_event(
        self,
        decision: RoutingDecision,
        request: Event,
    ) -> Event:
        """
        Build the `routing.unclassified` event.

        The event payload includes only the HASH of the
        input text (never the text itself — PII may be
        present; this event goes to a DLQ / fallback
        consumer that can be off-host). The `candidates`
        list is included for the LLM fallback path to
        know "the model was torn between X and Y".
        """
        text_hash = hashlib.sha256(
            _scalar_text(request.data.get("text")).encode("utf-8")
        ).hexdigest()
        payload = {
            "text_hash": text_hash,
            "threshold": self._config.threshold,
            "schema_version": decision.schema_version,
            "candidates": [
                {"label": lbl, "score": score} for lbl, score in decision.candidates
            ],
        }
        return Event.domain_from(
            agent_id=request.agent_id,
            type=EVENT_TYPE_ROUTING_UNCLASSIFIED,
            data=payload,
            correlation=request.correlation,
            causation_id=request.event_id,
        )

    @staticmethod
    def _compute_schema_version(labels: tuple[str, ...]) -> str:
        """
        Stable hash of the label set. Used as a cache key
        suffix and as a column in audit events. Recomputed
        on every construction, NOT cached externally.
        """
        joined = "|".join(sorted(labels))
        return short_hash(joined)


# ---------------------------------------------------------------------------
# Reactive system glue
# ---------------------------------------------------------------------------


def route_on_user_message(
    role: SemanticRoutingRole,
    event: Event,
) -> list[Event]:
    """
    Reactive system adapter for `user.message.received`.

    Synchronous wrapper: it calls the role's `classify`
    synchronously by delegating to an injected async
    runner when the dispatcher is async. The dispatcher
    contract in the framework expects a sync callable
    that returns a list of events to emit; the standard
    integration is to wrap this in an async handler.

    Returns the empty list for non-matching event types
    (the dispatcher fans out to many systems; this one
    only reacts to the configured `request_event_type`).

    This helper is intentionally sync to mirror the
    pattern in `kntgraph.agents/examples/11_tool_invoker.py`
    and the dispatcher's `ReactiveSystem` Protocol.
    Callers needing async gather both via
    `asyncio.run(role.classify(text))` or by exposing a
    thin async wrapper around this function.
    """
    if event.event_type != role.config.request_event_type:
        return []
    # NOTE: an async classifier cannot be driven from a
    # sync function. The recommended integration is to
    # expose an `async def` reactive system and have it
    # call `await role.classify(text)` then `role.build_event(...)`.
    # The sync version is here as a placeholder for
    # reactive systems that do not need async inference
    # (e.g. fake / mock classifier). Production callers
    # should use the async path.
    raise NotImplementedError(
        "Use `await role.classify(text)` then `role.build_event(...)` "
        "from an async reactive system. This sync helper exists for "
        "tests / sync dispatcher shapes only."
    )


async def async_route_on_user_message(
    role: SemanticRoutingRole,
    event: Event,
) -> list[Event]:
    """
    Async version of `route_on_user_message`. The
    recommended integration for a real dispatcher.

    Returns:
      - `[tool_event]` on a confident route;
      - `[unclassified_event]` on a below-threshold
        decision (the dispatcher routes this to the DLQ /
        LLM fallback);
      - `[]` on a non-matching event type.
      - `[]` on a `classify` Err (the role already
        wraps a hard error in `Err(ToolError(...))`;
        the caller should log it via `structlog`; we do
        not emit a `routing.unclassified` for hard
        classifier errors — that event is reserved for
        "no decision with available model", not for
        "model crashed".)
    """
    if event.event_type != role.config.request_event_type:
        return []
    result = await role.classify(_scalar_text(event.data.get("text", "")))
    if result.is_err():
        # Log via structlog; emit nothing. The dispatcher
        # is expected to have its own monitoring on Err
        # returns from systems.
        import structlog

        structlog.get_logger().error(
            "routing.classify_failed",
            error=str(result.err_value_or_raise()),
            request_event_id=str(event.event_id),
        )
        return []
    return [role.build_event(result.unwrap(), request=event)]


__all__ = [
    "EVENT_TYPE_USER_MESSAGE",
    "EVENT_TYPE_ROUTING_UNCLASSIFIED",
    "RoutingConfig",
    "RoutingDecision",
    "SemanticRoutingRole",
    "route_on_user_message",
    "async_route_on_user_message",
]


def _scalar_text(value: JsonValue) -> str:
    """Coerce a ``JsonValue`` to ``str``.

    Used in the deprecated role to feed the classifier
    and the ``text_hash`` digest. Non-scalar shapes
    (dict, list) are stringified for stability (the
    legacy classifier accepted any string).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return str(value)
