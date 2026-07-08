# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for code review item #9:
Event.from_dict and Event._parse_event (event_log.py) trust
the `event_class` field from the wire format without
validating it.

The framework relies on `event_class` being either
``"lifecycle"`` or ``"domain"`` for downstream dispatch
(ReactiveDispatcher filters by it; projection into
AgentView branches on it; the World fold relies on it).
A malformed value (from a buggy producer, a corrupted
Redis entry, or a malicious caller) would silently fall
through every system without raising.

These tests pin the contract that BOTH ``Event.create``
and ``Event.from_dict`` validate ``event_class`` at
runtime, and that the same applies to the byte-decoding
path used by the EventLog.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from kntgraph.core.event import (
    CorrelationContext,
    Event,
    OperationalEventType,
)


# ---------------------------------------------------------------------------
# Event.create — runtime validation of event_class
# ---------------------------------------------------------------------------


class TestEventCreateValidation:
    def test_create_accepts_lifecycle(self):
        e = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        assert e.event_class == "lifecycle"

    def test_create_accepts_domain(self):
        e = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        assert e.event_class == "domain"

    def test_create_rejects_unknown_event_class(self):
        """
        Before the fix, Event.create accepted any string
        for event_class. The Literal type hint was a static
        promise that nothing enforced. After the fix, the
        runtime must raise.
        """
        with pytest.raises(ValueError) as excinfo:
            Event.create(
                event_type="x",
                agent_id="a-1",
                event_class="banana",  # type: ignore[arg-type],
                correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
            )
        assert "event_class" in str(excinfo.value).lower()
        assert "banana" in str(excinfo.value)

    def test_create_rejects_empty_event_class(self):
        with pytest.raises(ValueError):
            Event.create(
                event_type="x",
                agent_id="a-1",
                event_class="",  # type: ignore[arg-type],
                correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
            )

    def test_create_rejects_uppercase_event_class(self):
        """The allowed set is lowercase; case is significant."""
        with pytest.raises(ValueError):
            Event.create(
                event_type="x",
                agent_id="a-1",
                event_class="LIFECYCLE",  # type: ignore[arg-type],
                correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
            )


class TestOperationFromValidation:
    """``operation_from`` is the framework's own builder."""

    def test_operation_from_pins_lifecycle(self):
        e = Event.operation_from(
            agent_id="a-1",
            type=OperationalEventType.SPAWNED,
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        assert e.event_class == "lifecycle"
        assert e.event_type == "agent.spawned"

    def test_domain_from_pins_domain(self):
        e = Event.domain_from(
            agent_id="a-1",
            type="document.received",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        assert e.event_class == "domain"


# ---------------------------------------------------------------------------
# Event.from_dict — runtime validation of event_class
# ---------------------------------------------------------------------------


class TestEventFromDictValidation:
    def _valid_dict(self, event_class: str = "domain") -> dict:
        eid = uuid.uuid4()
        return {
            "event_id": str(eid),
            "agent_id": "a-1",
            "event_type": "document.received",
            "event_class": event_class,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": {},
            "correlation": {
                "correlation_id": str(uuid.uuid4()),
                "causation_id": "",
                "span_id": str(uuid.uuid4()),
                "metadata": {},
            },
            "causation_id": "",
            "version": "1",
        }

    def test_from_dict_accepts_lifecycle(self):
        d = self._valid_dict("lifecycle")
        e = Event.from_dict(d)
        assert e.event_class == "lifecycle"

    def test_from_dict_accepts_domain(self):
        d = self._valid_dict("domain")
        e = Event.from_dict(d)
        assert e.event_class == "domain"

    def test_from_dict_rejects_unknown_event_class(self):
        """
        The wire format comes from Redis. A corrupted entry
        or a producer bug could put a value like
        ``"banana"`` here. The decode path must reject
        this rather than silently construct a malformed
        Event.
        """
        d = self._valid_dict("banana")
        with pytest.raises(ValueError) as excinfo:
            Event.from_dict(d)
        assert "event_class" in str(excinfo.value).lower()
        assert "banana" in str(excinfo.value)

    def test_from_dict_rejects_empty_event_class(self):
        d = self._valid_dict("")
        with pytest.raises(ValueError):
            Event.from_dict(d)

    def test_from_dict_rejects_missing_event_class(self):
        d = self._valid_dict("domain")
        del d["event_class"]
        with pytest.raises((ValueError, KeyError)):
            Event.from_dict(d)

    def test_from_json_rejects_invalid_event_class(self):
        """The full JSON path also validates."""
        d = self._valid_dict("not_a_class")
        import json

        s = json.dumps(d)
        with pytest.raises(ValueError):
            Event.from_json(s)


# ---------------------------------------------------------------------------
# Round-trip integrity
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_to_from_dict_preserves_event_class(self):
        original = Event.operation_from(
            agent_id="a-1",
            type=OperationalEventType.RUNNING,
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        roundtripped = Event.from_dict(original.to_dict())
        assert roundtripped.event_class == original.event_class
        assert roundtripped.event_type == original.event_type
        assert roundtripped.event_id == original.event_id
