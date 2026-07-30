# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``memory/continuity/recorders/entity.py``.

Closes the entity-recorder coverage gap (DEBT §3,
64% → 100%). The module exports a single pure function,
``build_entity_seen_event``, that builds a
``continuity.entity_seen`` event from the manager's raw
inputs. The PII gate (ADR-014 §2.7) is colocated here,
not on the manager: the recorder is the single place
that constructs the event.

The two branches of the function are covered below:

  - The happy path: a valid PII hash produces an
    ``Ok(Event)`` whose ``data`` carries the three
    fields and whose ``event_type`` is
    ``ContinuityEventType.ENTITY_SEEN``.
  - The PII gate rejection: a non-hash value produces
    ``Err(PersistenceError)`` without leaking the
    raw value into the error message.
"""

from __future__ import annotations

from kntgraph.core.event import CorrelationContext
from kntgraph.core.result import PersistenceError
from kntgraph.memory.continuity.recorders.entity import (
    build_entity_seen_event,
)
from kntgraph.memory.continuity.state import ContinuityEventType


class TestBuildEntitySeenEvent:
    def test_ok_for_valid_pii_hash(self):
        result = build_entity_seen_event(
            agent_id="continuity:tenant-a:user-1",
            correlation=CorrelationContext.new(),
            kind="document",
            value_hash="sha256:abcdef0123456789",
            source="ocr",
        )
        assert result.is_ok()
        event = result.ok_value()
        assert event.event_type == ContinuityEventType.ENTITY_SEEN
        assert event.agent_id == "continuity:tenant-a:user-1"
        assert event.data == {
            "kind": "document",
            "value_hash": "sha256:abcdef0123456789",
            "source": "ocr",
        }

    def test_err_for_raw_value(self):
        result = build_entity_seen_event(
            agent_id="continuity:tenant-a:user-1",
            correlation=CorrelationContext.new(),
            kind="document",
            value_hash="user@example.com",
            source="ocr",
        )
        assert result.is_err()
        assert isinstance(result.err_value(), PersistenceError)
        assert "sha256:" in str(result.err_value())

    def test_err_for_wrong_prefix(self):
        result = build_entity_seen_event(
            agent_id="continuity:tenant-a:user-1",
            correlation=CorrelationContext.new(),
            kind="document",
            value_hash="md5:abcdef",
            source="ocr",
        )
        assert result.is_err()
        assert isinstance(result.err_value(), PersistenceError)

    def test_err_for_empty_value(self):
        result = build_entity_seen_event(
            agent_id="continuity:tenant-a:user-1",
            correlation=CorrelationContext.new(),
            kind="document",
            value_hash="",
            source="ocr",
        )
        assert result.is_err()

    def test_err_message_does_not_leak_raw_value(self):
        raw = "leakable-raw-value"
        result = build_entity_seen_event(
            agent_id="continuity:tenant-a:user-1",
            correlation=CorrelationContext.new(),
            kind="document",
            value_hash=raw,
            source="ocr",
        )
        assert result.is_err()
        assert raw not in str(result.err_value())
