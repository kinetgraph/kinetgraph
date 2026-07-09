# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for code review item #10:
centralised event-shape validation in Event.create.

Before this fix, validation of event shape was spread
unevenly:

  - `Event.create` accepted any string for `event_type`
    (including the empty string) and any string for
    `agent_id` (also empty).
  - `Event.domain_from` validated `event_type` (non-empty,
    not "agent.*") but `create` did not.
  - `Event.operation_from` accepted only OperationalEventType
    (typed at the call site), so it was safe.
  - `__post_init__` (added in item #9) validates `event_class`.

The asymmetry meant: a caller using the public `create`
method directly could construct an event with
`event_type=""` and `agent_id=""`, which the rest of the
framework would not be able to route, fold, or display.

This fix centralises the shape validation in `Event.create`
(the canonical entry point — every other builder, plus
`from_dict`, calls it). Validation covers:

  - `event_type` is a non-empty string.
  - `agent_id` is a non-empty string.
  - `data` is a Mapping (not None, not a bare string).

These tests pin the new contract.
"""

from __future__ import annotations

import uuid

import pytest

from kntgraph.core.event import CorrelationContext, Event


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# event_type
# ---------------------------------------------------------------------------


class TestEventTypeValidation:
    def test_create_rejects_empty_event_type(self):
        with pytest.raises(ValueError) as excinfo:
            Event.create(
                event_type="",
                agent_id="a-1",
                event_class="domain",
                correlation=_ctx(),
            )
        assert "event_type" in str(excinfo.value)

    def test_create_rejects_non_string_event_type(self):
        with pytest.raises(TypeError):
            Event.create(
                event_type=123,  # type: ignore[arg-type]
                agent_id="a-1",
                event_class="domain",
                correlation=_ctx(),
            )

    def test_domain_from_already_validates_empty(self):
        """Pre-existing behaviour, asserted as a regression guard."""
        from kntgraph.core.event import Event

        with pytest.raises(TypeError):
            Event.domain_from(agent_id="a-1", type="")

    def test_domain_from_already_rejects_agent_namespace(self):
        """Pre-existing behaviour, asserted as a regression guard."""
        from kntgraph.core.event import Event

        with pytest.raises(ValueError) as excinfo:
            Event.domain_from(agent_id="a-1", type="agent.something")
        assert "agent.something" in str(excinfo.value)


# ---------------------------------------------------------------------------
# agent_id
# ---------------------------------------------------------------------------


class TestAgentIdValidation:
    def test_create_rejects_empty_agent_id(self):
        with pytest.raises(ValueError) as excinfo:
            Event.create(
                event_type="document.received",
                agent_id="",
                event_class="domain",
                correlation=_ctx(),
            )
        assert "agent_id" in str(excinfo.value)

    def test_create_rejects_non_string_agent_id(self):
        with pytest.raises(TypeError):
            Event.create(
                event_type="x",
                agent_id=None,  # type: ignore[arg-type]
                event_class="domain",
                correlation=_ctx(),
            )

    def test_from_dict_rejects_empty_agent_id(self):
        """Wire decoder must catch empty agent_id (a corrupted entry)."""
        d = {
            "event_id": "00000000-0000-0000-0000-000000000000",
            "agent_id": "",
            "event_type": "x",
            "event_class": "domain",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "data": {},
            "correlation": {
                "correlation_id": "00000000-0000-0000-0000-000000000000",
                "causation_id": "",
                "span_id": "00000000-0000-0000-0000-000000000000",
                "metadata": {},
            },
            "causation_id": "",
            "version": "1",
        }
        with pytest.raises(ValueError):
            Event.from_dict(d)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


class TestDataValidation:
    def test_create_accepts_empty_dict(self):
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="domain",
            data={},
            correlation=_ctx(),
        )
        assert e.data == {}

    def test_create_accepts_none_data(self):
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="domain",
            correlation=_ctx(),
        )
        assert e.data == {}

    def test_create_rejects_non_mapping_data(self):
        """
        The `data` payload must be a Mapping. A bare string
        or a list would silently break the JSON
        serialisation later.
        """
        with pytest.raises(TypeError):
            Event.create(
                event_type="x",
                agent_id="a-1",
                event_class="domain",
                data="not a mapping",  # type: ignore[arg-type]
                correlation=_ctx(),
            )

    def test_create_rejects_list_data(self):
        with pytest.raises(TypeError):
            Event.create(
                event_type="x",
                agent_id="a-1",
                event_class="domain",
                data=["a", "b"],  # type: ignore[arg-type]
                correlation=_ctx(),
            )


# ---------------------------------------------------------------------------
# Round-trip integrity (no regression)
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_valid_event_survives_roundtrip(self):
        e = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"k": "v"},
            correlation=_ctx(),
        )
        s = e.to_json()
        e2 = Event.from_json(s)
        assert e2.event_type == e.event_type
        assert e2.agent_id == e2.agent_id
        assert e2.event_class == e.event_class
        assert e2.data == e.data
        assert e2.event_id == e.event_id
