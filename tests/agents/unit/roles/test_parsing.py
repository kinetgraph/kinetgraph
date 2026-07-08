# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the JSON parser used by Roles.

Local models (gemma, llama, etc.) often wrap JSON in
markdown code blocks. The parser must extract the JSON
even from those wrappers.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from kntgraph.agents.roles._parsing import extract_json, parse_model_json


class Item(BaseModel):
    name: str
    count: int


class TestExtractJson:
    def test_plain_json(self):
        text = '{"name": "x", "count": 1}'
        assert extract_json(text) == text

    def test_markdown_fence(self):
        text = '```json\n{"name": "x", "count": 1}\n```'
        assert extract_json(text) == '{"name": "x", "count": 1}'

    def test_fence_no_lang(self):
        text = '```\n{"name": "x", "count": 1}\n```'
        assert extract_json(text) == '{"name": "x", "count": 1}'

    def test_fence_with_surrounding_text(self):
        text = 'Here is the result:\n\n```json\n{"name": "x", "count": 1}\n```\n\nDone.'
        assert extract_json(text) == '{"name": "x", "count": 1}'

    def test_object_embedded_in_prose(self):
        text = 'Sure! The answer is {"name": "x", "count": 1}. OK?'
        assert extract_json(text) == '{"name": "x", "count": 1}'

    def test_no_json_returns_input(self):
        text = "no json here"
        assert extract_json(text) == text


class TestParseModelJson:
    def test_plain(self):
        r = parse_model_json('{"name": "x", "count": 1}', Item)
        assert r.name == "x" and r.count == 1

    def test_markdown_fence(self):
        text = '```json\n{"name": "y", "count": 42}\n```'
        r = parse_model_json(text, Item)
        assert r.name == "y" and r.count == 42

    def test_prose_around_fence(self):
        text = (
            "Here is your JSON:\n"
            "```json\n"
            '{"name": "z", "count": 7}\n'
            "```\n"
            "Anything else?"
        )
        r = parse_model_json(text, Item)
        assert r.name == "z" and r.count == 7

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            parse_model_json("not json at all", Item)
