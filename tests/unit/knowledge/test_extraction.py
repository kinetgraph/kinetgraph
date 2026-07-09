# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `knowledge/extraction/` — the Entity extraction
package (ADR-010 §3, Fase 2).

The tests cover:

  - `Entity` dataclass invariants (canonical name,
    canonical key, attributes).
  - `canonicalize` (the case-fold + whitespace-collapse
    helper).
  - `parse_payload` (free text vs JSON dict).
  - `dedup_entities` (preserves first-seen order).
  - `HeuristicEntityExtractor` (regex + payload scan,
    including dedup, empty input, multi-line text).
  - `GlinerEntityAdapter` (template method; default
    returns `[]`; subclass with canned spans works;
    conversion handles malformed input).
  - `EntityExtractor` / `EntityExtractorWithMentions`
    Protocol runtime checks.

The tests are pure (no Redis, no FalkorDB, no GLiNER2
model). The GLiNER2 stub returns `[]` until a real
model is wired in.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kntgraph.knowledge.extraction import (
    Entity,
    EntityExtractor,
    EntityExtractorWithMentions,
    HeuristicEntityExtractor,
    GlinerEntityAdapter,
    canonicalize,
    dedup_entities,
    parse_payload,
)
from kntgraph.knowledge.extraction.base import (
    ENTITY_TYPE_ID,
    ENTITY_TYPE_MONEY,
    ENTITY_TYPE_DATE,
)


# ---------------------------------------------------------------------------
# canonicalize
# ---------------------------------------------------------------------------


class TestCanonicalize:
    def test_strips_outer_whitespace(self) -> None:
        assert canonicalize("  hello  ") == "hello"

    def test_collapses_internal_whitespace(self) -> None:
        assert canonicalize("a   b   c") == "a b c"

    def test_lowercases(self) -> None:
        assert canonicalize("ACME S/A") == "acme s/a"

    def test_empty_returns_empty(self) -> None:
        assert canonicalize("") == ""
        assert canonicalize("   ") == ""

    def test_keeps_punctuation(self) -> None:
        # Punctuation is significant (legal name).
        assert canonicalize("ACME S/A") != canonicalize("ACME SA")

    def test_keeps_digits(self) -> None:
        assert canonicalize("NF-00123") == "nf-00123"


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class TestEntity:
    def test_canonical_name_applied(self) -> None:
        e = Entity(name="  ACME S/A  ", type="org")
        assert e.name == "acme s/a"
        assert e.canonical_key == ("acme s/a", "org")

    def test_canonical_name_idempotent(self) -> None:
        e = Entity(name="acme", type="org")
        assert e.name == "acme"

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            Entity(name="", type="org")

    def test_empty_type_raises(self) -> None:
        with pytest.raises(ValueError, match="type must be non-empty"):
            Entity(name="x", type="")

    def test_surface_preserved(self) -> None:
        # Surface stays verbatim; only `name` is canonical.
        e = Entity(name="ACME", type="org", surface="  ACME S/A  ")
        assert e.surface == "  ACME S/A  "
        assert e.name == "acme"

    def test_attributes_default_empty(self) -> None:
        e = Entity(name="x", type="id")
        assert e.attributes == {}


# ---------------------------------------------------------------------------
# dedup_entities
# ---------------------------------------------------------------------------


class TestDedup:
    def test_first_seen_wins(self) -> None:
        a = Entity(name="acme", type="org", surface="ACME")
        b = Entity(name="acme", type="org", surface="Acmé")
        out = dedup_entities([a, b])
        assert len(out) == 1
        assert out[0].surface == "ACME"

    def test_different_types_kept(self) -> None:
        a = Entity(name="acme", type="org")
        b = Entity(name="acme", type="other")
        out = dedup_entities([a, b])
        assert len(out) == 2

    def test_empty(self) -> None:
        assert dedup_entities([]) == []


# ---------------------------------------------------------------------------
# parse_payload
# ---------------------------------------------------------------------------


class TestParsePayload:
    def test_json_object(self) -> None:
        assert parse_payload(json.dumps({"a": 1})) == {"a": 1}

    def test_non_json(self) -> None:
        assert parse_payload("hello world") is None

    def test_json_array_is_not_dict(self) -> None:
        assert parse_payload("[1, 2, 3]") is None

    def test_empty(self) -> None:
        assert parse_payload("") is None

    def test_invalid_json(self) -> None:
        assert parse_payload("{not valid}") is None


# ---------------------------------------------------------------------------
# HeuristicEntityExtractor
# ---------------------------------------------------------------------------


class TestHeuristicExtractor:
    def setup_method(self) -> None:
        self.ext = HeuristicEntityExtractor()

    def test_extracts_cnpj(self) -> None:
        ents = asyncio.run(self.ext.extract("CNPJ 12.345.678/0001-90 emite NF"))
        cnpjs = [e for e in ents if e.name == "12.345.678/0001-90"]
        assert len(cnpjs) == 1
        assert cnpjs[0].type == ENTITY_TYPE_ID

    def test_extracts_money_literal(self) -> None:
        ents = asyncio.run(self.ext.extract("total R$ 1.234,56"))
        money = [e for e in ents if e.type == ENTITY_TYPE_MONEY]
        assert any(e.name == "r$ 1.234,56" for e in money)

    def test_extracts_iso_date(self) -> None:
        ents = asyncio.run(self.ext.extract("emitida em 2024-05-12"))
        dates = [e for e in ents if e.type == ENTITY_TYPE_DATE]
        assert any(e.name == "2024-05-12" for e in dates)

    def test_extracts_short_id(self) -> None:
        ents = asyncio.run(self.ext.extract("NF-00123 e INVC/2024/0001"))
        ids = [e for e in ents if e.type == ENTITY_TYPE_ID]
        names = {e.name for e in ids}
        assert "nf-00123" in names
        assert "invc/2024/0001" in names

    def test_payload_scan_known_keys(self) -> None:
        payload = {
            "supplier": "ACME S/A",
            "cnpj": "12.345.678/0001-90",
            "amount": 1234.56,
            "issue_date": "2024-05-12",
        }
        ents = asyncio.run(self.ext.extract(json.dumps(payload, sort_keys=True)))
        tags_by_type = {e.type: e for e in ents}
        assert tags_by_type["org"].name == "acme s/a"
        assert tags_by_type["id"].name == "12.345.678/0001-90"
        assert tags_by_type["money"].name == "1234.56"
        assert tags_by_type["date"].name == "2024-05-12"

    def test_payload_dedup_text_and_structured(self) -> None:
        # Same CNPJ matches in both passes → dedup.
        payload = {"cnpj": "12.345.678/0001-90"}
        ents = asyncio.run(self.ext.extract(json.dumps(payload, sort_keys=True)))
        cnpjs = [e for e in ents if e.name == "12.345.678/0001-90"]
        assert len(cnpjs) == 1

    def test_payload_lists_not_walked(self) -> None:
        payload = {
            "supplier": "ACME",
            "entries": ["a", "b", "c"],  # ignored
        }
        ents = asyncio.run(self.ext.extract(json.dumps(payload)))
        # No entity from inside `entries`.
        assert all("a" not in e.name for e in ents if e.type == "other")

    def test_empty_input(self) -> None:
        assert asyncio.run(self.ext.extract("")) == []
        assert asyncio.run(self.ext.extract("   ")) == []
        assert asyncio.run(self.ext.extract("not json")) == []

    def test_pure_same_input_same_output(self) -> None:
        text = "NF-001 cnpj 12.345.678/0001-90 valor R$ 100,00"
        a = asyncio.run(self.ext.extract(text))
        b = asyncio.run(self.ext.extract(text))
        assert [(e.type, e.name) for e in a] == [(e.type, e.name) for e in b]


# ---------------------------------------------------------------------------
# GlinerEntityAdapter (template method)
# ---------------------------------------------------------------------------


class TestGlinerExtractor:
    def test_default_returns_empty(self) -> None:
        ext = GlinerEntityAdapter()
        assert asyncio.run(ext.extract("anything")) == []
        assert asyncio.run(ext.extract_with_mentions("anything")) == []

    def test_validates_labels(self) -> None:
        with pytest.raises(ValueError, match="labels must be non-empty"):
            GlinerEntityAdapter(labels=())
        with pytest.raises(ValueError, match="labels must be non-empty strings"):
            GlinerEntityAdapter(labels=("",))

    def test_validates_threshold(self) -> None:
        with pytest.raises(ValueError, match="threshold must be in"):
            GlinerEntityAdapter(threshold=1.5)
        with pytest.raises(ValueError, match="threshold must be in"):
            GlinerEntityAdapter(threshold=-0.1)

    def test_subclass_can_inject_spans(self) -> None:
        class FakeGliner(GlinerEntityAdapter):
            async def _run_model(self, text):
                return [
                    self._convert_span(s)
                    for s in [
                        {
                            "text": "ACME",
                            "label": "org",
                            "start": 0,
                            "score": 0.95,
                        },
                        {
                            "text": "12.345.678/0001-90",
                            "label": "id",
                            "start": 10,
                            "score": 0.99,
                        },
                        {"text": "", "label": "org"},  # malformed
                    ]
                ]

        ext = FakeGliner()
        ents = asyncio.run(ext.extract("ACME cnpj 12.345.678/0001-90"))
        assert len(ents) == 2
        assert {e.name for e in ents} == {
            "acme",
            "12.345.678/0001-90",
        }
        # score attribute preserved
        scored = [e for e in ents if "gliner_score" in e.attributes]
        assert all(0.0 <= e.attributes["gliner_score"] <= 1.0 for e in scored)

    def test_convert_span_handles_object_shape(self) -> None:
        class SpanObj:
            text = "ACME S/A"
            label = "org"
            start = 5
            score = 0.9

        ext = GlinerEntityAdapter()
        e, offset = ext._convert_span(SpanObj())
        assert e is not None
        assert e.name == "acme s/a"
        assert e.type == "org"
        assert offset == 5
        assert e.attributes["gliner_score"] == 0.9

    def test_convert_span_malformed(self) -> None:
        ext = GlinerEntityAdapter()
        assert ext._convert_span({"text": "", "label": "org"}) is None
        assert ext._convert_span({"text": "x", "label": ""}) is None
        assert ext._convert_span({"label": "org"}) is None

    def test_satisfies_with_mentions_protocol(self) -> None:
        ext = GlinerEntityAdapter()
        assert isinstance(ext, EntityExtractorWithMentions)
        assert isinstance(ext, EntityExtractor)
