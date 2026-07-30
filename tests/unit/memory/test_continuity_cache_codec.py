# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``memory/continuity/cache_codec.py``.

Closes the cache-codec coverage gap (DEBT §3,
19% reported as 74% on the global run because the
serialiser is on the hot path of the fold test, but
the standalone module-level coverage is 19% — the
uncovered branches are the bytes-keyed normaliser,
the slot extractor, the coerce helpers' non-string
arms, and the cache-miss paths).

The codec is pure: no Redis client, no fakeredis. The
two public functions plus the three private helpers
are exercised below.
"""

from __future__ import annotations

from kntgraph.core._typing import JsonValue
from kntgraph.memory.continuity.cache_codec import (
    read_cache,
    serialize_for_cache,
)
from kntgraph.memory.continuity.state import (
    MAX_FIELD_VALUE_LEN,
    ContinuityState,
)


# ---------------------------------------------------------------------------
# serialize_for_cache
# ---------------------------------------------------------------------------


class TestSerializeForCache:
    def test_scalars_always_emitted(self):
        state = ContinuityState(
            tenant_id="t",
            user_id="u",
            created_at=100.0,
            updated_at=200.0,
        )
        mapping = serialize_for_cache(state)
        assert mapping["tenant_id"] == "t"
        assert mapping["user_id"] == "u"
        assert mapping["created_at"] == "100.0"
        assert mapping["updated_at"] == "200.0"
        assert "cleared_at" not in mapping

    def test_cleared_at_emitted_when_set(self):
        state = ContinuityState(
            tenant_id="t",
            user_id="u",
            created_at=100.0,
            updated_at=200.0,
            cleared_at=300.0,
        )
        mapping = serialize_for_cache(state)
        assert mapping["cleared_at"] == "300.0"

    def test_tool_slots_use_tool_prefix(self):
        state = ContinuityState(
            tenant_id="t",
            user_id="u",
            last_tools={"ocr": "sig-1", "nlp": "sig-2"},
        )
        mapping = serialize_for_cache(state)
        assert mapping["tool:ocr"] == "sig-1"
        assert mapping["tool:nlp"] == "sig-2"

    def test_entity_slots_use_entity_prefix(self):
        state = ContinuityState(
            tenant_id="t",
            user_id="u",
            last_entities={"NF-001": "1700000000.0"},
        )
        mapping = serialize_for_cache(state)
        assert mapping["entity:NF-001"] == "1700000000.0"

    def test_category_slots_use_last_prefix(self):
        state = ContinuityState(
            tenant_id="t",
            user_id="u",
            last_categories={"categoria-A": "valor"},
        )
        mapping = serialize_for_cache(state)
        assert mapping["last:categoria-A"] == "valor"

    def test_values_truncated_to_max_field_value_len(self):
        long_value = "x" * (MAX_FIELD_VALUE_LEN + 100)
        state = ContinuityState(
            tenant_id="t",
            user_id="u",
            last_tools={"ocr": long_value},
        )
        mapping = serialize_for_cache(state)
        assert len(mapping["tool:ocr"]) == MAX_FIELD_VALUE_LEN


# ---------------------------------------------------------------------------
# read_cache
# ---------------------------------------------------------------------------


class TestReadCache:
    def test_none_for_empty_mapping(self):
        assert read_cache({}) is None
        assert read_cache({}.items() and {}) is None  # type: ignore[arg-type]

    def test_none_when_no_created_at(self):
        raw = {"tenant_id": "t", "user_id": "u", "updated_at": "200.0"}
        assert read_cache(raw) is None

    def test_minimal_state_round_trips(self):
        raw = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": "100.0",
            "updated_at": "200.0",
        }
        state = read_cache(raw)
        assert state is not None
        assert state.tenant_id == "t"
        assert state.user_id == "u"
        assert state.created_at == 100.0
        assert state.updated_at == 200.0
        assert state.cleared_at is None
        assert state.last_tools == {}
        assert state.last_entities == {}
        assert state.last_categories == {}

    def test_cleared_at_parsed(self):
        raw = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": "100.0",
            "updated_at": "200.0",
            "cleared_at": "300.0",
        }
        state = read_cache(raw)
        assert state is not None
        assert state.cleared_at == 300.0
        assert state.is_cleared() is True

    def test_cleared_at_none_when_unparseable(self):
        raw = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": "100.0",
            "updated_at": "200.0",
            "cleared_at": "not-a-number",
        }
        state = read_cache(raw)
        assert state is not None
        assert state.cleared_at is None

    def test_tenant_id_kwarg_overrides_payload(self):
        raw = {
            "tenant_id": "wrong",
            "user_id": "wrong",
            "created_at": "100.0",
            "updated_at": "200.0",
        }
        state = read_cache(raw, tenant_id="right-t", user_id="right-u")
        assert state is not None
        assert state.tenant_id == "right-t"
        assert state.user_id == "right-u"

    def test_slots_round_trip(self):
        raw = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": "100.0",
            "updated_at": "200.0",
            "tool:ocr": "sig-1",
            "entity:NF-001": "1700000000.0",
            "last:categoria-A": "valor",
        }
        state = read_cache(raw)
        assert state is not None
        assert state.last_tools == {"ocr": "sig-1"}
        assert state.last_entities == {"NF-001": "1700000000.0"}
        assert state.last_categories == {"categoria-A": "valor"}

    def test_bytes_keyed_payload_decodes(self):
        raw = {
            b"tenant_id": b"t",
            b"user_id": b"u",
            b"created_at": b"100.0",
            b"updated_at": b"200.0",
        }
        state = read_cache(raw)
        assert state is not None
        assert state.tenant_id == "t"
        assert state.created_at == 100.0

    def test_string_keyed_payload_with_json_values(self):
        raw: dict[str, JsonValue] = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": 100.0,
            "updated_at": 200.0,
        }
        state = read_cache(raw)
        assert state is not None
        assert state.created_at == 100.0

    def test_json_int_created_at(self):
        raw: dict[str, JsonValue] = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": 100,
            "updated_at": 200,
        }
        state = read_cache(raw)
        assert state is not None
        assert state.created_at == 100.0
        assert state.updated_at == 200.0

    def test_json_bool_created_at(self):
        raw: dict[str, JsonValue] = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": True,
            "updated_at": 0.0,
        }
        state = read_cache(raw)
        assert state is not None
        assert state.created_at == 0.0

    def test_json_int_cleared_at(self):
        raw: dict[str, JsonValue] = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": 100.0,
            "updated_at": 200.0,
            "cleared_at": 300,
        }
        state = read_cache(raw)
        assert state is not None
        assert state.cleared_at == 300.0

    def test_json_bool_cleared_at(self):
        raw: dict[str, JsonValue] = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": 100.0,
            "updated_at": 200.0,
            "cleared_at": False,
        }
        state = read_cache(raw)
        assert state is not None
        assert state.cleared_at is None

    def test_cleared_at_with_unparseable_string(self):
        raw: dict[str, JsonValue] = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": 100.0,
            "updated_at": 200.0,
            "cleared_at": ["a", "b"],
        }
        state = read_cache(raw)
        assert state is not None
        assert state.cleared_at is None

    def test_created_at_with_unparseable_string(self):
        raw: dict[str, JsonValue] = {
            "tenant_id": "t",
            "user_id": "u",
            "created_at": "not-a-number",
            "updated_at": ["a", "b"],
        }
        state = read_cache(raw)
        assert state is not None
        assert state.created_at == 0.0
        assert state.updated_at == 0.0


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_serialize_then_read_recovers_state(self):
        original = ContinuityState(
            tenant_id="t",
            user_id="u",
            last_tools={"ocr": "sig-1", "nlp": "sig-2"},
            last_entities={"NF-001": "1700000000.0"},
            last_categories={"categoria-A": "valor"},
            created_at=100.0,
            updated_at=200.0,
            cleared_at=None,
        )
        mapping = serialize_for_cache(original)
        state = read_cache(mapping, tenant_id="t", user_id="u")
        assert state is not None
        assert state.tenant_id == "t"
        assert state.user_id == "u"
        assert state.last_tools == original.last_tools
        assert state.last_entities == original.last_entities
        assert state.last_categories == original.last_categories
        assert state.created_at == 100.0
        assert state.updated_at == 200.0
        assert state.cleared_at is None
