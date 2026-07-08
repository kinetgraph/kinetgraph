# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for SemanticRoutingRole (ADR-013, Momento 1).

The role is exercised against a fake `IntentClassifier`
that returns canned `Classification` values. This keeps
tests deterministic and free of GLiNER2 model loading.

Coverage:
  - Construction snapshots the registry's tool names.
  - Construction with empty registry is allowed.
  - Schema version is stable for the same label set,
    different when labels change.
  - classify(): high score → routed decision.
  - classify(): low score → unclassified decision.
  - classify(): empty input → Err.
  - classify(): empty registry → unclassified Ok.
  - classify(): classifier raises → Err (role boundary).
  - build_event(): routed path emits
    `tool.{name}.requested` with the right payload.
  - build_event(): unclassified path emits
    `routing.unclassified` with the candidate list and
    text_hash (NOT the text).
  - build_event(): event_id is deterministic for the
    same (request, decision).
  - async_route_on_user_message(): matches the
    configured request_event_type.
  - async_route_on_user_message(): ignores other event
    types.
  - async_route_on_user_message(): logs but emits
    nothing on classify error.
  - RoutingConfig.from_env(): env vars override
    defaults; malformed values fall back.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Optional
from unittest.mock import patch
from uuid import uuid4

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.knowledge.extraction import (
    Classification,
    IntentClassifier,
    IntentScore,
)
from kntgraph.agents.tools.protocol import Tool, ToolRegistry, ToolEventType

from kntgraph.agents.roles import (
    EVENT_TYPE_ROUTING_UNCLASSIFIED,
    RoutingConfig,
    RoutingDecision,
    SemanticRoutingRole,
    async_route_on_user_message,
)


# Async test methods are marked individually. Sync tests
# (TestConstruction, TestRoutingConfig) are intentionally
# left unmarked: the kntgraph.agents pyproject sets
# `asyncio_mode = "strict"`, which requires an explicit
# mark on every `async def test_*` but rejects stray marks
# on sync `def test_*`.


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeTool(Tool):
    """Minimal Tool for populating a ToolRegistry in tests."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description or f"tool {name}"
        self.input_schema: dict = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
        }
        self.invocations: list[dict] = []

    async def invoke(self, *, idempotency_key: str, **kwargs):
        self.invocations.append({"idempotency_key": idempotency_key, **kwargs})
        from kntgraph.core.result import Ok

        return Ok({"ok": True})


class FakeClassifier(IntentClassifier):
    """
    Deterministic in-memory classifier for tests.

    The `responses` dict maps text → Classification. If
    `text` is missing, the classifier raises (use
    `queue_error` to control which text triggers a raise).
    """

    def __init__(self) -> None:
        self.responses: dict[str, Classification] = {}
        self.error_texts: set[str] = set()
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def queue(
        self,
        text: str,
        *,
        top_label: str,
        top_score: float,
        candidates: Iterable[tuple[str, float]] = (),
    ) -> None:
        cands = tuple(IntentScore(label=lbl, score=score) for lbl, score in candidates)
        self.responses[text] = Classification(
            top_label=top_label,
            top_score=top_score,
            candidates=cands,
        )

    def queue_no_decision(self, text: str) -> None:
        self.responses[text] = Classification(
            top_label="", top_score=0.0, candidates=()
        )

    def queue_error(self, text: str) -> None:
        self.error_texts.add(text)

    async def classify(
        self,
        text: str,
        labels: Iterable[str],
        descriptions: Optional[Iterable[str]] = None,
    ) -> Classification:
        labels_tuple = tuple(labels)
        self.calls.append((text, labels_tuple))
        if text in self.error_texts:
            raise RuntimeError("fake classifier boom")
        if text in self.responses:
            return self.responses[text]
        # Default: empty / no decision. Lets a test
        # drive the empty-registry or default branch.
        return Classification(top_label="", top_score=0.0, candidates=())


def _build_registry(names: Iterable[str]) -> ToolRegistry:
    reg = ToolRegistry()
    for n in names:
        reg.register(_FakeTool(n))
    return reg


def _user_message_event(
    text: str,
    *,
    agent_id: str = "agent-1",
    correlation: CorrelationContext | None = None,
) -> Event:
    """
    Build a synthetic `user.message.received` event for tests.

    `correlation` defaults to a fresh `CorrelationContext`
    so tests don't have to thread one through. Tests that
    need a specific correlation (e.g. to assert that the
    routed event inherits the same `correlation_id`) can
    pass one in. ADR-037: `correlation` is required on
    `Event.domain_from`; the default satisfies it for the
    common case.
    """
    if correlation is None:
        correlation = CorrelationContext.new(correlation_id=uuid4())
    return Event.domain_from(
        agent_id=agent_id,
        type="user.message.received",
        data={"text": text},
        correlation=correlation,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_snapshots_tool_names(self) -> None:
        reg = _build_registry(["emitir_nfe", "cancelar_nfe", "consultar_status"])
        clf = FakeClassifier()
        role = SemanticRoutingRole(reg, clf)
        assert role.labels == (
            "emitir_nfe",
            "cancelar_nfe",
            "consultar_status",
        )

    def test_empty_registry_is_allowed(self) -> None:
        reg = ToolRegistry()
        clf = FakeClassifier()
        role = SemanticRoutingRole(reg, clf)
        assert role.labels == ()
        # schema_version is still a stable hash (of empty input).
        assert isinstance(role.schema_version, str) and len(role.schema_version) == 16

    def test_schema_version_changes_with_labels(self) -> None:
        clf = FakeClassifier()
        a = SemanticRoutingRole(_build_registry(["x", "y"]), clf)
        b = SemanticRoutingRole(_build_registry(["x", "z"]), clf)
        assert a.schema_version != b.schema_version

    def test_schema_version_stable_for_same_labels(self) -> None:
        clf = FakeClassifier()
        a = SemanticRoutingRole(_build_registry(["x", "y"]), clf)
        b = SemanticRoutingRole(_build_registry(["y", "x"]), clf)
        # Sorted: order-independent.
        assert a.schema_version == b.schema_version

    def test_requires_registry_and_classifier(self) -> None:
        clf = FakeClassifier()
        reg = _build_registry(["x"])
        with pytest.raises(ValueError, match="registry is required"):
            SemanticRoutingRole(None, clf)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="classifier is required"):
            SemanticRoutingRole(reg, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.asyncio
    async def test_high_score_routes_to_tool(self) -> None:
        reg = _build_registry(["emitir_nfe", "cancelar_nfe"])
        clf = FakeClassifier()
        clf.queue(
            "quero emitir uma NF-e",
            top_label="emitir_nfe",
            top_score=0.92,
            candidates=[("emitir_nfe", 0.92), ("cancelar_nfe", 0.05)],
        )
        role = SemanticRoutingRole(reg, clf, config=RoutingConfig(threshold=0.6))
        r = await role.classify("quero emitir uma NF-e")
        assert r.is_ok()
        d: RoutingDecision = r.unwrap()
        assert d.target_tool == "emitir_nfe"
        assert d.confidence == pytest.approx(0.92)
        assert d.is_unclassified is False
        # Candidates truncated per config (default top_k=3).
        assert ("emitir_nfe", pytest.approx(0.92)) in d.candidates

    @pytest.mark.asyncio
    async def test_low_score_is_unclassified(self) -> None:
        reg = _build_registry(["emitir_nfe", "cancelar_nfe"])
        clf = FakeClassifier()
        clf.queue(
            "sei lá",
            top_label="emitir_nfe",
            top_score=0.31,
            candidates=[("emitir_nfe", 0.31), ("cancelar_nfe", 0.30)],
        )
        role = SemanticRoutingRole(reg, clf, config=RoutingConfig(threshold=0.6))
        r = await role.classify("sei lá")
        assert r.is_ok()
        d = r.unwrap()
        assert d.target_tool == ""
        assert d.is_unclassified is True
        assert d.confidence == pytest.approx(0.31)

    @pytest.mark.asyncio
    async def test_empty_input_returns_err(self) -> None:
        reg = _build_registry(["x"])
        clf = FakeClassifier()
        role = SemanticRoutingRole(reg, clf)
        r = await role.classify("")
        assert r.is_err()
        assert "empty" in str(r.err_value())
        r2 = await role.classify("   ")
        assert r2.is_err()

    @pytest.mark.asyncio
    async def test_empty_registry_returns_unclassified_ok(self) -> None:
        reg = ToolRegistry()
        clf = FakeClassifier()
        role = SemanticRoutingRole(reg, clf)
        r = await role.classify("anything")
        assert r.is_ok()
        d = r.unwrap()
        assert d.is_unclassified is True
        assert d.target_tool == ""

    @pytest.mark.asyncio
    async def test_classifier_exception_becomes_err(self) -> None:
        reg = _build_registry(["x"])
        clf = FakeClassifier()
        clf.queue_error("kaboom")
        role = SemanticRoutingRole(reg, clf)
        r = await role.classify("kaboom")
        assert r.is_err()
        assert "classifier_error" in str(r.err_value())

    @pytest.mark.asyncio
    async def test_classifier_receives_snapshot_labels(self) -> None:
        reg = _build_registry(["emitir_nfe", "cancelar_nfe"])
        clf = FakeClassifier()
        clf.queue("hi", top_label="emitir_nfe", top_score=0.9)
        role = SemanticRoutingRole(reg, clf)
        await role.classify("hi")
        # The classifier saw the snapshotted labels, in
        # registry insertion order.
        assert clf.calls == [("hi", ("emitir_nfe", "cancelar_nfe"))]

    @pytest.mark.asyncio
    async def test_unclassified_when_top_label_empty(self) -> None:
        reg = _build_registry(["x"])
        clf = FakeClassifier()
        clf.queue_no_decision("?")
        role = SemanticRoutingRole(reg, clf)
        r = await role.classify("?")
        assert r.is_ok()
        d = r.unwrap()
        assert d.is_unclassified is True
        assert d.target_tool == ""


# ---------------------------------------------------------------------------
# build_event()
# ---------------------------------------------------------------------------


class TestBuildEvent:
    @pytest.mark.asyncio
    async def test_routed_event_has_correct_type_and_payload(self) -> None:
        reg = _build_registry(["emitir_nfe", "cancelar_nfe"])
        clf = FakeClassifier()
        clf.queue(
            "emitir",
            top_label="emitir_nfe",
            top_score=0.9,
            candidates=[("emitir_nfe", 0.9)],
        )
        role = SemanticRoutingRole(reg, clf)
        decision = (await role.classify("emitir")).unwrap()
        request = _user_message_event("emitir", agent_id="agent-1")
        event = role.build_event(decision, request=request)
        assert event.event_type == ToolEventType.requested("emitir_nfe")
        assert event.causation_id == request.event_id
        assert event.data["args"] == {}
        assert event.data["routing"]["confidence"] == pytest.approx(0.9)
        assert event.data["routing"]["schema_version"] == role.schema_version
        # Inherits correlation from the request.
        assert event.correlation.correlation_id == request.correlation.correlation_id

    @pytest.mark.asyncio
    async def test_unclassified_event_has_hash_not_text(self) -> None:
        reg = _build_registry(["emitir_nfe"])
        clf = FakeClassifier()
        # The text the user sent is "meu cpf é ..." (PII).
        # The classifier ran on it, returned low confidence
        # with the top-2 surfaced for the fallback path.
        text = "meu cpf é 123.456.789-00"
        clf.queue(
            text,
            top_label="emitir_nfe",
            top_score=0.2,
            candidates=[("emitir_nfe", 0.2), ("cancelar_nfe", 0.18)],
        )
        role = SemanticRoutingRole(reg, clf, config=RoutingConfig(threshold=0.6))
        decision = (await role.classify(text)).unwrap()
        assert decision.is_unclassified
        request = _user_message_event(text)
        event = role.build_event(decision, request=request)
        assert event.event_type == EVENT_TYPE_ROUTING_UNCLASSIFIED
        # PII hygiene: text is hashed, never stored.
        assert "text" not in event.data
        expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert event.data["text_hash"] == expected_hash
        assert event.data["threshold"] == pytest.approx(0.6)
        # candidates surfaced for fallback.
        labels_in_event = {c["label"] for c in event.data["candidates"]}
        assert "emitir_nfe" in labels_in_event
        assert "cancelar_nfe" in labels_in_event

    @pytest.mark.asyncio
    async def test_routed_event_id_is_deterministic(self) -> None:
        reg = _build_registry(["x"])
        clf = FakeClassifier()
        clf.queue("hi", top_label="x", top_score=0.9)
        role = SemanticRoutingRole(reg, clf)
        decision = (await role.classify("hi")).unwrap()
        request = _user_message_event("hi")
        e1 = role.build_event(decision, request=request)
        e2 = role.build_event(decision, request=request)
        assert e1.event_id == e2.event_id

    @pytest.mark.asyncio
    async def test_unclassified_event_id_is_deterministic(self) -> None:
        reg = _build_registry(["x"])
        clf = FakeClassifier()
        clf.queue("hi", top_label="x", top_score=0.1)
        role = SemanticRoutingRole(reg, clf, config=RoutingConfig(threshold=0.6))
        decision = (await role.classify("hi")).unwrap()
        request = _user_message_event("hi")
        e1 = role.build_event(decision, request=request)
        e2 = role.build_event(decision, request=request)
        assert e1.event_id == e2.event_id


# ---------------------------------------------------------------------------
# Reactive glue
# ---------------------------------------------------------------------------


class TestReactiveGlue:
    @pytest.mark.asyncio
    async def test_async_route_routes_on_match(self) -> None:
        reg = _build_registry(["emitir_nfe", "cancelar_nfe"])
        clf = FakeClassifier()
        clf.queue("emitir", top_label="emitir_nfe", top_score=0.9)
        role = SemanticRoutingRole(reg, clf)
        request = _user_message_event("emitir")
        out = await async_route_on_user_message(role, request)
        assert len(out) == 1
        assert out[0].event_type == ToolEventType.requested("emitir_nfe")

    @pytest.mark.asyncio
    async def test_async_route_ignores_other_event_types(self) -> None:
        reg = _build_registry(["x"])
        clf = FakeClassifier()
        role = SemanticRoutingRole(reg, clf)
        other = Event.domain_from(
            agent_id="agent-1",
            type="something.else",
            data={"text": "x"},
            correlation=CorrelationContext.new(correlation_id=uuid4()),
        )
        out = await async_route_on_user_message(role, other)
        assert out == []

    @pytest.mark.asyncio
    async def test_async_route_emits_unclassified_on_low_score(self) -> None:
        reg = _build_registry(["emitir_nfe"])
        clf = FakeClassifier()
        clf.queue("???", top_label="emitir_nfe", top_score=0.2)
        role = SemanticRoutingRole(reg, clf, config=RoutingConfig(threshold=0.6))
        request = _user_message_event("???")
        out = await async_route_on_user_message(role, request)
        assert len(out) == 1
        assert out[0].event_type == EVENT_TYPE_ROUTING_UNCLASSIFIED

    @pytest.mark.asyncio
    async def test_async_route_emits_nothing_on_classifier_error(self) -> None:
        reg = _build_registry(["x"])
        clf = FakeClassifier()
        clf.queue_error("kaboom")
        role = SemanticRoutingRole(reg, clf)
        request = _user_message_event("kaboom")
        # Hard error: the reactive system logs and emits
        # nothing. The DLQ is reserved for "no decision",
        # not "model crashed".
        with patch("structlog.get_logger") as get_logger:
            logger = get_logger.return_value
            out = await async_route_on_user_message(role, request)
        assert out == []
        logger.error.assert_called_once()
        kwargs = logger.error.call_args.kwargs
        assert "routing.classify_failed" in kwargs.get(
            "event", ""
        ) or "routing.classify_failed" in (logger.error.call_args.args or ())


# ---------------------------------------------------------------------------
# RoutingConfig
# ---------------------------------------------------------------------------


class TestRoutingConfig:
    def test_default_threshold_is_0_6(self) -> None:
        cfg = RoutingConfig()
        assert cfg.threshold == pytest.approx(0.6)
        assert cfg.top_k_candidates == 3

    def test_validates_threshold(self) -> None:
        with pytest.raises(ValueError, match="threshold must be in"):
            RoutingConfig(threshold=1.5)
        with pytest.raises(ValueError, match="threshold must be in"):
            RoutingConfig(threshold=-0.1)

    def test_validates_top_k(self) -> None:
        with pytest.raises(ValueError, match="top_k_candidates must be > 0"):
            RoutingConfig(top_k_candidates=0)

    def test_from_env_uses_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in ("FMH_ROUTING_THRESHOLD", "FMH_ROUTING_TOP_K_CANDIDATES"):
            monkeypatch.delenv(k, raising=False)
        cfg = RoutingConfig.from_env()
        assert cfg.threshold == pytest.approx(0.6)
        assert cfg.top_k_candidates == 3

    def test_from_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FMH_ROUTING_THRESHOLD", "0.75")
        monkeypatch.setenv("FMH_ROUTING_TOP_K_CANDIDATES", "5")
        cfg = RoutingConfig.from_env()
        assert cfg.threshold == pytest.approx(0.75)
        assert cfg.top_k_candidates == 5

    def test_from_env_malformed_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        A malformed env value MUST raise instead of
        silently falling back to the default. The previous
        behaviour (silent fallback) masked operator typos
        such as `FMH_ROUTING_THRESHOLD=0,6` (comma in
        pt-BR) and led to routing running with the
        default threshold of 0.6 when the operator
        believed it was 0.75. The `Settings`-backed
        loader surfaces the mistake at startup.
        """
        from pydantic import ValidationError

        monkeypatch.setenv("FMH_ROUTING_THRESHOLD", "not-a-float")
        monkeypatch.setenv("FMH_ROUTING_TOP_K_CANDIDATES", "not-an-int")
        with pytest.raises(ValidationError):
            RoutingConfig.from_env()
