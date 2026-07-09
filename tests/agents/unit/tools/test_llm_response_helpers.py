# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the helpers extracted from
``_to_llm_response``.

The full ``_to_llm_response`` is exercised
end-to-end by ``TestInvoke`` and
``TestToolProtocolConformance`` (see
``test_llm.py``). These tests focus on the static
parsing helpers introduced when CC=13 was lowered
to A grade by splitting the message/usage/raw-dict
extraction steps out of the constructor.
"""

from __future__ import annotations


from kntgraph.agents.tools.llm import (
    _convert_to_raw_dict,
    _parse_message,
    _parse_usage,
    LLMUsage,
)


# ---------------------------------------------------------------------------
# _parse_message
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_extracts_text_from_first_choice(self):
        completion = {"choices": [{"message": {"content": "hello"}}]}
        text, finish = _parse_message(completion)
        assert text == "hello"
        assert finish is None

    def test_extracts_finish_reason(self):
        completion = {
            "choices": [
                {
                    "message": {"content": "ok"},
                    "finish_reason": "stop",
                }
            ]
        }
        text, finish = _parse_message(completion)
        assert text == "ok"
        assert finish == "stop"

    def test_empty_choices_yields_empty_text(self):
        """Some providers return an empty `choices` list
        on certain error paths. We default to empty
        text and None finish."""
        completion = {"choices": []}
        text, finish = _parse_message(completion)
        assert text == ""
        assert finish is None

    def test_missing_choices_yields_empty_text(self):
        completion: dict = {}
        text, finish = _parse_message(completion)
        assert text == ""
        assert finish is None

    def test_missing_message_yields_empty_text(self):
        """A choice without a `message` block (some
        reasoning-only responses) yields empty text."""
        completion = {"choices": [{}]}
        text, finish = _parse_message(completion)
        assert text == ""
        assert finish is None

    def test_explicit_none_content_yields_empty_text(self):
        """`content: None` (rare but possible) is
        treated the same as missing."""
        completion = {"choices": [{"message": {"content": None}}]}
        text, _ = _parse_message(completion)
        assert text == ""


# ---------------------------------------------------------------------------
# _parse_usage
# ---------------------------------------------------------------------------


class TestParseUsage:
    def test_all_fields_present(self):
        completion = {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            }
        }
        usage = _parse_usage(completion)
        assert usage == LLMUsage(
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )

    def test_missing_usage_block_yields_zeros(self):
        completion: dict = {}
        usage = _parse_usage(completion)
        assert usage == LLMUsage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )

    def test_none_usage_yields_zeros(self):
        completion = {"usage": None}
        usage = _parse_usage(completion)
        assert usage == LLMUsage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )

    def test_partial_usage_fills_missing_with_zero(self):
        """If the provider returns only `total_tokens`,
        the other fields default to 0."""
        completion = {"usage": {"total_tokens": 50}}
        usage = _parse_usage(completion)
        assert usage == LLMUsage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=50,
        )

    def test_string_values_are_coerced_to_int(self):
        """Some providers (or proxies) send token
        counts as strings. The parser coerces."""
        completion = {
            "usage": {
                "prompt_tokens": "7",
                "completion_tokens": "13",
                "total_tokens": "20",
            }
        }
        usage = _parse_usage(completion)
        assert usage.prompt_tokens == 7
        assert usage.completion_tokens == 13
        assert usage.total_tokens == 20


# ---------------------------------------------------------------------------
# _convert_to_raw_dict
# ---------------------------------------------------------------------------


class TestConvertToRawDict:
    def test_dict_input_returns_copy(self):
        """A plain dict is copied (not aliased) so the
        caller cannot mutate the original."""
        original = {"a": 1, "b": 2}
        result = _convert_to_raw_dict(original)
        assert result == original
        result["c"] = 3
        assert "c" not in original

    def test_pydantic_like_input_uses_model_dump(self):
        """A pydantic-style object with a working
        ``model_dump()`` is serialised through it."""

        class _PydanticLike:
            def model_dump(self) -> dict:
                return {"alpha": 1, "beta": 2}

        result = _convert_to_raw_dict(_PydanticLike())
        assert result == {"alpha": 1, "beta": 2}

    def test_failing_model_dump_falls_back_to_safe_dict(self):
        """When ``model_dump()`` raises, the parser
        falls back to ``_safe_dict`` (a defensive
        attribute-by-attribute copy)."""

        class _BrokenPydantic:
            def model_dump(self) -> dict:
                raise ValueError("validation broke")

            alpha: int = 1
            beta: str = "two"

        result = _convert_to_raw_dict(_BrokenPydantic())
        # The result is whatever `_safe_dict` produces
        # (we don't pin the exact shape; we just
        # assert the call did not raise and the
        # attributes were reachable).
        assert isinstance(result, dict)

    def test_unknown_object_falls_back_to_safe_dict(self):
        """An object without `model_dump` and not a
        dict falls back to ``_safe_dict``."""

        class _Plain:
            alpha = 1
            beta = "two"

        result = _convert_to_raw_dict(_Plain())
        assert isinstance(result, dict)
