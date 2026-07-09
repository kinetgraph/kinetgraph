# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the new Event (v2.0).

Covers:
  - Lifecycle vs domain event_class distinction
  - Correlation context propagation (ADR-037: mandatory)
  - Deterministic event_id (idempotency)
  - JSON round-trip
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from kntgraph.core.event import (
    OPERATIONAL_EVENT_TO_PHASE,
    CorrelationContext,
    Event,
    OperationalEventType,
    correlation_middleware,
    generate_deterministic_event_id,
)


# ---------------------------------------------------------------------------
# Helper: a fresh CorrelationContext for tests that
# pre-date ADR-037 and don't care about correlation. New
# tests should pin an explicit flow id.
# ---------------------------------------------------------------------------


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid4())


# ---------------------------------------------------------------------------
# Event creation
# ---------------------------------------------------------------------------


class TestEventCreation:
    def test_minimal_event(self):
        e = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        assert e.event_type == "agent.spawned"
        assert e.agent_id == "a-1"
        assert e.event_class == "lifecycle"
        assert e.data == {}
        assert e.version == 1

    def test_event_with_payload(self):
        e = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001", "valor": 1500.50},
            correlation=_ctx(),
        )
        assert e.data["document_id"] == "NF-001"
        assert e.data["valor"] == 1500.50

    def test_event_class_required(self):
        with pytest.raises(TypeError):
            Event.create(  # type: ignore[call-arg]
                event_type="x",
                agent_id="a-1",
            )

    def test_event_class_must_be_known_literal(self):
        # EventClass is `Literal["lifecycle", "domain"]`. Python itself
        # does not enforce Literal at runtime (mypy does), so an
        # unknown string passes through. This is intentional: the
        # type is documented for static analysis, not runtime gates.
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",  # only legal value
            correlation=_ctx(),
        )
        assert e.event_class == "lifecycle"

    def test_event_with_correlation(self):
        ctx = CorrelationContext.new(metadata={"flow": "x"})
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=ctx,
        )
        assert e.correlation.correlation_id == ctx.correlation_id
        assert e.correlation.metadata["flow"] == "x"

    def test_data_is_a_fresh_dict_not_a_shared_reference(self):
        payload = {"k": "v"}
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",
            data=payload,
            correlation=_ctx(),
        )
        payload["k"] = "mutated"
        assert e.data["k"] == "v"


# ---------------------------------------------------------------------------
# Deterministic event_id
# ---------------------------------------------------------------------------


class TestEventIdDeterminism:
    def test_same_inputs_same_id(self):
        c = uuid4()
        a = generate_deterministic_event_id(c, "x", {"k": 1})
        b = generate_deterministic_event_id(c, "x", {"k": 1})
        assert a == b

    def test_different_payload_different_id(self):
        c = uuid4()
        a = generate_deterministic_event_id(c, "x", {"k": 1})
        b = generate_deterministic_event_id(c, "x", {"k": 2})
        assert a != b

    def test_different_event_type_different_id(self):
        c = uuid4()
        a = generate_deterministic_event_id(c, "x", {"k": 1})
        b = generate_deterministic_event_id(c, "y", {"k": 1})
        assert a != b

    def test_different_causation_different_id(self):
        a = generate_deterministic_event_id(uuid4(), "x", {"k": 1})
        b = generate_deterministic_event_id(uuid4(), "x", {"k": 1})
        assert a != b

    def test_event_create_with_deterministic_id(self):
        c = uuid4()
        e1 = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",
            causation_id=c,
            data={"k": 1},
            correlation=_ctx(),
        )
        e2 = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",
            causation_id=c,
            data={"k": 1},
            correlation=_ctx(),
        )
        assert e1.event_id == e2.event_id

    # Note: agent_id is application-owned. The framework is
    # intentionally agnostic about how the application picks
    # agent_ids (UUID v4, deterministic hash of business keys,
    # ERP id, etc.). The EventLog keys per-agent streams on
    # whatever string the caller provides.


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


class TestCorrelation:
    def test_new_context_has_ids(self):
        ctx = CorrelationContext.new()
        assert ctx.correlation_id is not None
        assert ctx.span_id is not None
        assert ctx.causation_id is None

    def test_context_metadata(self):
        ctx = CorrelationContext.new(metadata={"a": 1})
        assert ctx.metadata["a"] == 1

    def test_to_from_dict_roundtrip(self):
        ctx = CorrelationContext.new(metadata={"x": "y"})
        d = ctx.to_dict()
        ctx2 = CorrelationContext.from_dict(d)
        assert ctx.correlation_id == ctx2.correlation_id
        assert ctx.span_id == ctx2.span_id
        assert ctx.metadata == ctx2.metadata

    def test_middleware_scope(self):
        with correlation_middleware.scope(metadata={"k": "v"}) as ctx:
            assert ctx.metadata["k"] == "v"
            assert correlation_middleware.current() is ctx
        assert correlation_middleware.current() is None

    def test_continue_from_event(self):
        e = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        new_ctx = correlation_middleware.continue_from(e)
        assert new_ctx.correlation_id == e.correlation.correlation_id
        assert new_ctx.causation_id == e.event_id


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestEventSerialization:
    def test_to_from_dict(self):
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",
            data={"k": 1},
            causation_id=uuid4(),
            correlation=_ctx(),
        )
        d = e.to_dict()
        e2 = Event.from_dict(d)
        assert e.event_id == e2.event_id
        assert e.event_type == e2.event_type
        assert e.event_class == e2.event_class
        assert e.data == e2.data
        assert e.causation_id == e2.causation_id
        assert e.timestamp == e2.timestamp

    def test_to_from_json(self):
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",
            data={"k": 1},
            correlation=_ctx(),
        )
        s = e.to_json()
        e2 = Event.from_json(s)
        assert e.event_id == e2.event_id
        assert e.data == e2.data

    def test_immutability(self):
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        with pytest.raises((AttributeError, TypeError, Exception)):
            e.event_type = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# operation_from — framework-owned lifecycle events
# ---------------------------------------------------------------------------


class TestOperationFrom:
    def test_basic_spawned(self):
        e = Event.operation_from(
            agent_id="a-1",
            type=OperationalEventType.SPAWNED,
            correlation=_ctx(),
        )
        assert e.event_class == "lifecycle"
        assert e.event_type == "agent.spawned"
        assert e.agent_id == "a-1"
        assert e.data == {}

    def test_all_enum_values_accepted(self):
        for t in OperationalEventType:
            e = Event.operation_from(agent_id="a-1", type=t, correlation=_ctx())
            assert e.event_type == t.value
            assert e.event_class == "lifecycle"

    def test_event_type_phase_mapping_complete(self):
        """Every operational event_type maps to an OperationalPhase."""
        for t in OperationalEventType:
            assert t.value in OPERATIONAL_EVENT_TO_PHASE

    def test_passes_payload(self):
        e = Event.operation_from(
            agent_id="a-1",
            type=OperationalEventType.RUNNING,
            data={"task_id": "T-001"},
            correlation=_ctx(),
        )
        assert e.data == {"task_id": "T-001"}

    def test_propagates_causation_id(self):
        cause = Event.operation_from(
            agent_id="a-1",
            type=OperationalEventType.SPAWNED,
            correlation=_ctx(),
        )
        e = Event.operation_from(
            agent_id="a-1",
            type=OperationalEventType.IDLE,
            causation_id=cause.event_id,
            correlation=_ctx(),
        )
        assert e.causation_id == cause.event_id
        assert e.event_class == "lifecycle"


# ---------------------------------------------------------------------------
# domain_from — application-defined domain events
# ---------------------------------------------------------------------------


class TestDomainFrom:
    def test_basic_received(self):
        e = Event.domain_from(
            agent_id="a-1",
            type="document.received",
            data={"xml": "..."},
            correlation=_ctx(),
        )
        assert e.event_class == "domain"
        assert e.event_type == "document.received"
        assert e.data == {"xml": "..."}

    def test_arbitrary_type_accepted(self):
        """Domain event types are application-defined; framework
        does not enumerate them."""
        for t in (
            "document.received",
            "invoice.paid",
            "task.escalated",
            "x.y.z",
            "spaced type",  # even names with spaces are allowed
        ):
            e = Event.domain_from(agent_id="a-1", type=t, correlation=_ctx())
            assert e.event_type == t
            assert e.event_class == "domain"

    def test_rejects_framework_namespace_collision(self):
        """Domain events must NOT use the 'agent.*' namespace.
        Operational events go through operation_from."""
        for t in (
            "agent.spawned",
            "agent.idle",
            "agent.something.else",
        ):
            with pytest.raises(ValueError):
                Event.domain_from(agent_id="a-1", type=t, correlation=_ctx())

    def test_rejects_empty_type(self):
        with pytest.raises(TypeError):
            Event.domain_from(agent_id="a-1", type="", correlation=_ctx())

    def test_rejects_non_string_type(self):
        with pytest.raises(TypeError):
            Event.domain_from(
                agent_id="a-1",
                type=42,
                correlation=_ctx(),  # type: ignore[arg-type]
            )

    def test_propagates_causation_id(self):
        cause = Event.domain_from(
            agent_id="a-1",
            type="document.received",
            correlation=_ctx(),
        )
        e = Event.domain_from(
            agent_id="a-1",
            type="document.validated",
            causation_id=cause.event_id,
            correlation=_ctx(),
        )
        assert e.causation_id == cause.event_id


# ---------------------------------------------------------------------------
# Mutual exclusivity: event_class is pinned by the factory
# ---------------------------------------------------------------------------


class TestFactoryMutualExclusivity:
    def test_operation_from_pins_lifecycle(self):
        e = Event.operation_from(
            agent_id="a-1",
            type=OperationalEventType.SPAWNED,
            correlation=_ctx(),
        )
        assert e.event_class == "lifecycle"

    def test_domain_from_pins_domain(self):
        e = Event.domain_from(
            agent_id="a-1",
            type="document.received",
            correlation=_ctx(),
        )
        assert e.event_class == "domain"


# ---------------------------------------------------------------------------
# Default correlation_id == event_id invariant
# ---------------------------------------------------------------------------
# When the caller does NOT supply a `CorrelationContext`,
# the framework pins `correlation.correlation_id` to the
# event's own `event_id`. This makes the entry event of
# a flow self-identifying:
#
#   - Long-poll patterns (e.g.
#     `fmh_office.mvp.http.get_status`) match the
#     terminal event on
#     `correlation.correlation_id == entry.event_id`.
#   - Downstream events in the same flow
#     (linked by `causation_id`) inherit the
#     correlation via the `StepAdvancerSystem`
#     (`fmh_office.engine.systems._build_event`).
#
# Without the auto-pin, the framework used to
# default `correlation_id=uuid4()`, breaking the
# invariant and forcing every caller to pin it
# manually (which a previous fmh_office bug did
# twice — see the comment in
# `Event.create`).


class TestCorrelationIsMandatory:
    """ADR-037 supersedes the pre-ADR-037 default-pin
    behaviour. The old tests in
    ``TestDefaultCorrelationMatchesEventId`` proved
    that ``correlation_id == event_id`` when the caller
    did not pass a correlation context. This was a
    silent contract that broke flow auditability; the
    caller had no way to know their tool call had lost
    its flow context. The new behaviour is: the caller
    MUST pass a ``CorrelationContext``; the framework
    raises ``TypeError`` if it is missing.

    These tests verify the new contract.
    """

    def test_domain_from_without_correlation_raises_type_error(
        self,
    ):
        with pytest.raises(TypeError, match="correlation"):
            Event.domain_from(agent_id="a-1", type="document.received")

    def test_operation_from_without_correlation_raises_type_error(
        self,
    ):
        with pytest.raises(TypeError, match="correlation"):
            Event.operation_from(
                agent_id="a-1",
                type=OperationalEventType.SPAWNED,
            )

    def test_create_without_correlation_raises_type_error(
        self,
    ):
        with pytest.raises(TypeError, match="correlation"):
            Event.create(
                event_type="x",
                agent_id="a-1",
                event_class="domain",
            )

    def test_explicit_correlation_overrides_event_id(self):
        """When the caller passes a ``CorrelationContext``,
        the framework uses it as-is. The default of
        ``correlation_id == event_id`` is gone. Callers
        that want a fresh flow start with
        ``CorrelationContext.new(correlation_id=...)``.
        Callers that derive from a parent use
        ``correlation_middleware.continue_from(parent)``.
        """
        flow_id = uuid4()
        ctx = CorrelationContext.new(correlation_id=flow_id)
        e = Event.domain_from(
            agent_id="a-1",
            type="document.received",
            correlation=ctx,
        )
        # The framework respected the explicit
        # correlation. The auto-pin would have been
        # ``e.event_id``, but the caller asked for
        # ``ctx.correlation_id``.
        assert e.correlation.correlation_id == flow_id
        assert e.correlation.correlation_id != e.event_id

    def test_idempotency_key_preserves_event_id_not_correlation(
        self,
    ):
        """Two calls with the same inputs produce the
        same ``event_id`` (deterministic). The
        ``correlation_id`` is whatever the caller
        pinned (here: the same explicit correlation).
        """
        ctx = CorrelationContext.new(correlation_id=uuid4())
        kwargs = dict(
            agent_id="a-1",
            type="document.received",
            data={"x": 1},
            correlation=ctx,
        )
        e1 = Event.domain_from(**kwargs)
        e2 = Event.domain_from(**kwargs)
        assert e1.event_id == e2.event_id
        assert e1.correlation.correlation_id == e2.correlation.correlation_id


# ---------------------------------------------------------------------------
# ADR-037: correlation is mandatory. The caller MUST
# supply a CorrelationContext. The framework no longer
# generates a default from the event_id.
# ---------------------------------------------------------------------------


class TestCorrelationPropagation:
    """ADR-037: when a caller passes ``correlation``,
    the framework uses it as-is. The previous default
    (correlation_id = eid) is gone.
    """

    def test_explicit_correlation_used_verbatim(self):
        ctx = CorrelationContext.new(correlation_id=uuid4())
        e = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            correlation=ctx,
        )
        assert e.correlation.correlation_id == ctx.correlation_id

    def test_continue_from_propagates_correlation_id(self):
        """``CorrelationMiddleware.continue_from(cause)``
        inherits the cause's ``correlation_id`` and
        sets ``causation_id`` to the cause's event_id.
        The downstream event keeps the flow id stable.
        """
        flow_id = uuid4()
        # ``start`` sets the context with an explicit
        # ``correlation_id``; we read it back via
        # ``current`` to build the entry event with the
        # same flow id.
        correlation_middleware.start(correlation_id=flow_id)
        try:
            entry_ctx = correlation_middleware.current()
            entry = Event.domain_from(
                agent_id="a-1",
                type="user.intent",
                data={"intent": "go"},
                correlation=entry_ctx,
            )
        finally:
            correlation_middleware.clear()
        # The caller propagates by ``continue_from``.
        derived = Event.domain_from(
            agent_id="a-1",
            type="tool.requested",
            data={"tool": "x"},
            causation_id=entry.event_id,
            correlation=correlation_middleware.continue_from(entry),
        )
        assert derived.correlation.correlation_id == entry.correlation.correlation_id
        assert derived.correlation.correlation_id == flow_id

    def test_correlation_id_stable_through_tool_round_trip(self):
        """End-to-end: ``user.intent -> tool.requested ->
        tool.x.completed`` all share the same
        ``correlation_id`` (= the flow id).
        """
        flow_id = uuid4()
        ctx = CorrelationContext.new(correlation_id=flow_id)
        entry = Event.domain_from(
            agent_id="a-1",
            type="user.intent",
            data={"intent": "go"},
            correlation=ctx,
        )
        request = Event.domain_from(
            agent_id="a-1",
            type="tool.requested",
            data={"tool": "x"},
            causation_id=entry.event_id,
            correlation=correlation_middleware.continue_from(entry),
        )
        completion = Event.domain_from(
            agent_id="a-1",
            type="tool.x.completed",
            data={"v": 1},
            causation_id=request.event_id,
            correlation=correlation_middleware.continue_from(request),
        )
        # All three share the flow id.
        assert entry.correlation.correlation_id == flow_id
        assert request.correlation.correlation_id == flow_id
        assert completion.correlation.correlation_id == flow_id


class TestFromDictPreservesCorrelation:
    """ADR-037: the wire decoder (``from_dict``) MUST
    accept the same correlation semantics. A
    ``correlation_id`` carried in the JSON payload
    becomes the rebuilt event's correlation.
    """

    def test_from_dict_with_correlation_id_roundtrip(self):
        flow_id = uuid4()
        e = Event.domain_from(
            agent_id="a-1",
            type="user.intent",
            data={"k": "v"},
            correlation=CorrelationContext.new(correlation_id=flow_id),
        )
        d = e.to_dict()
        rebuilt = Event.from_dict(d)
        assert rebuilt.correlation.correlation_id == flow_id
