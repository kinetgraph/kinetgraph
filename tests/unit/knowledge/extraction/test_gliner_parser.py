# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
#

"""
Behaviour tests for the GLiNER2 output parser.

``extract_first`` is the boundary between what the model
returns and what the framework does with it, and it is
**shape-tolerant by design**: GLiNER2 changed its return
shape across versions (1.3.x canonical dict, pre-1.3
dataclass list, bare strings, top-level label map).

The shapes below are the ones the library actually
produces -- ``gliner2.inference.runtime.format_entity_dict``
emits a **list** of matches per label, and
``format_results`` emits ``{"label": ..., "confidence": ...}``
for classification tasks. Pinning them here is what keeps
the finder from silently finding nothing.
"""

from __future__ import annotations

import pytest

from kntgraph.knowledge.extraction.argument._gliner_finder import (
    extract_first,
    match_to_value,
)


class TestExtractFirst:
    def test_canonical_list_of_matches(self) -> None:
        """
        The 1.3.x canonical shape: a LIST of matches per
        label. The first match is the one the framework
        takes (the library sorts the spans by confidence).
        """
        raw = {
            "entities": {
                "org": [
                    {"text": "ACME", "confidence": 0.93},
                    {"text": "ACME LTDA", "confidence": 0.41},
                ]
            }
        }
        assert extract_first(raw, "org") == ("ACME", pytest.approx(0.93))

    def test_single_match_per_label(self) -> None:
        """A single match (the pre-1.3 shape) still works."""
        raw = {"entities": {"org": {"text": "ACME", "confidence": 0.8}}}
        assert extract_first(raw, "org") == ("ACME", pytest.approx(0.8))

    def test_bare_string_match(self) -> None:
        """
        Without ``include_confidence`` the library returns
        plain strings. The framework reports 1.0 so the
        downstream threshold does not drop them.
        """
        raw = {"entities": {"org": ["ACME"]}}
        assert extract_first(raw, "org") == ("ACME", 1.0)

    def test_top_level_label_map(self) -> None:
        """The older top-level ``{label: [...]}`` shape."""
        raw = {"org": [{"text": "ACME", "confidence": 0.7}]}
        assert extract_first(raw, "org") == ("ACME", pytest.approx(0.7))

    def test_candidates_list(self) -> None:
        """The older list-of-candidates shape."""
        raw = [{"label": "org", "text": "ACME", "score": 0.6}]
        assert extract_first(raw, "org") == ("ACME", pytest.approx(0.6))

    def test_unknown_label_returns_none(self) -> None:
        """A label the model did not find is not an error."""
        raw = {"entities": {"org": [{"text": "ACME", "confidence": 0.9}]}}
        assert extract_first(raw, "person") is None

    def test_empty_result_returns_none(self) -> None:
        """No entities at all — the finder reports nothing."""
        assert extract_first({"entities": {}}, "org") is None


class TestMatchToValue:
    def test_dict_match(self) -> None:
        assert match_to_value({"text": "ACME", "confidence": 0.5}) == (
            "ACME",
            pytest.approx(0.5),
        )

    def test_bare_string_match(self) -> None:
        """No confidence available — reported as 1.0."""
        assert match_to_value("ACME") == ("ACME", 1.0)

    def test_none_is_none(self) -> None:
        assert match_to_value(None) is None
