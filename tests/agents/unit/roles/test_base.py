# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for `_BaseLLMRole`.

The base is exercised indirectly by the four role
subclasses (ChatRole, PlannerRole, SummarizerRole,
PersonalizedRole). These tests focus on the shared
behaviour that does not depend on a specific role:

  - Empty-input validation
  - Stable idempotency key
  - JSON parsing helper
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from kntgraph.core.result import Err, Ok

from kntgraph.agents.roles._base import _BaseLLMRole


pytestmark = pytest.mark.asyncio


class _SampleModel(BaseModel):
    """Tiny model used to exercise `_parse_json`."""

    name: str
    value: int = Field(default=0)


class _NoopRole(_BaseLLMRole):
    """Concrete subclass used to drive the base methods."""

    DEFAULT_MAX_TOKENS = 16
    DEFAULT_TEMPERATURE = 0.0
    OUTPUT_PREFIX = "noop"


class TestCheckInput:
    def test_empty_string_rejected(self):
        role = _NoopRole(llm=None)  # type: ignore[arg-type]
        r = role._check_input("", "task")
        assert r is not None
        assert r.is_err()
        assert "empty task" in str(r.err_value())

    def test_whitespace_only_rejected(self):
        role = _NoopRole(llm=None)  # type: ignore[arg-type]
        r = role._check_input("   \n\t  ", "user message")
        assert r is not None
        assert r.is_err()
        assert "empty user message" in str(r.err_value())

    def test_non_empty_returns_none(self):
        """`_check_input` returns `None` (no error) for
        non-empty input. The caller pattern is
        `if (err := self._check_input(...)) is not None: return err`."""
        role = _NoopRole(llm=None)  # type: ignore[arg-type]
        assert role._check_input("hello", "task") is None
        assert role._check_input("  ok  ", "task") is None


class TestStableKey:
    def test_same_inputs_produce_same_key(self):
        k1 = _NoopRole._stable_key("a", "b", "c")
        k2 = _NoopRole._stable_key("a", "b", "c")
        assert k1 == k2

    def test_different_inputs_produce_different_keys(self):
        k1 = _NoopRole._stable_key("a", "b")
        k2 = _NoopRole._stable_key("a", "c")
        assert k1 != k2

    def test_key_has_prefix_and_length(self):
        """The key is `{prefix}:{32 hex chars}` —
        matches the contract documented in
        `_BaseLLMRole._stable_key` and used by the
        role subclasses."""
        k = _NoopRole._stable_key("x")
        assert k.startswith("noop:")
        # `prefix:` + 32 hex chars
        assert len(k) == len("noop:") + 32
        # Hex chars after the colon
        assert all(c in "0123456789abcdef" for c in k.split(":", 1)[1])

    def test_different_prefix_per_subclass(self):
        """Each role subclasses with its own
        `OUTPUT_PREFIX` so the same input produces a
        different key (avoids cache collisions
        between roles)."""

        class _OtherRole(_BaseLLMRole):
            OUTPUT_PREFIX = "other"

        k1 = _NoopRole._stable_key("same", "input")
        k2 = _OtherRole._stable_key("same", "input")
        assert k1.startswith("noop:")
        assert k2.startswith("other:")
        assert k1 != k2

    def test_parts_coerced_to_string(self):
        """Non-string parts are coerced via `str()`
        so the hash is deterministic across types."""
        k1 = _NoopRole._stable_key("a", 1)
        k2 = _NoopRole._stable_key("a", "1")
        # Both produce the same key because the int
        # is stringified to "1".
        assert k1 == k2

    def test_different_part_count_different_key(self):
        k1 = _NoopRole._stable_key("a", "b")
        k2 = _NoopRole._stable_key("a", "b", "c")
        assert k1 != k2


class TestParseJson:
    def test_valid_json_parses(self):
        role = _NoopRole(llm=None)  # type: ignore[arg-type]
        r = role._parse_json('{"name": "x", "value": 1}', _SampleModel, "parse_error")
        assert r.is_ok()
        assert r.unwrap().name == "x"
        assert r.unwrap().value == 1

    def test_invalid_json_returns_typed_error(self):
        role = _NoopRole(llm=None)  # type: ignore[arg-type]
        r = role._parse_json("not json", _SampleModel, "parse_error")
        assert r.is_err()
        assert "parse_error" in str(r.err_value())

    def test_validation_error_returns_typed_error(self):
        """JSON is well-formed but the model rejects
        the payload (missing required field)."""
        role = _NoopRole(llm=None)  # type: ignore[arg-type]
        r = role._parse_json('{"value": 1}', _SampleModel, "model_error")
        assert r.is_err()
        assert "model_error" in str(r.err_value())

    def test_markdown_fenced_json_parses(self):
        """`parse_model_json` strips ```json fences."""
        role = _NoopRole(llm=None)  # type: ignore[arg-type]
        text = '```json\n{"name": "y", "value": 2}\n```'
        r = role._parse_json(text, _SampleModel, "parse_error")
        assert r.is_ok()
        assert r.unwrap().name == "y"


class TestInvoke:
    """The `_invoke` helper forwards to `self._llm.invoke`
    with the role's standard kwargs. This test uses a
    fake `LiteLLMTool` to verify the call shape."""

    async def test_invoke_passes_role_config(self):
        from unittest.mock import AsyncMock

        fake_llm = AsyncMock()
        fake_llm.invoke = AsyncMock(return_value=Ok(_FakeResponse("ok")))
        role = _NoopRole(
            llm=fake_llm,  # type: ignore[arg-type]
            model="custom",
            max_tokens=64,
            temperature=0.5,
        )
        r = await role._invoke("sys", "user", key="k1")
        assert r.is_ok()
        fake_llm.invoke.assert_awaited_once()
        kwargs = fake_llm.invoke.await_args.kwargs
        assert kwargs["system"] == "sys"
        assert kwargs["user"] == "user"
        assert kwargs["idempotency_key"] == "k1"
        assert kwargs["model"] == "custom"
        assert kwargs["max_tokens"] == 64
        assert kwargs["temperature"] == 0.5

    async def test_invoke_forwards_extra_kwargs(self):
        """`think=False` (Ollama) and other
        `LiteLLMTool.invoke`-specific kwargs are
        forwarded."""
        from unittest.mock import AsyncMock

        fake_llm = AsyncMock()
        fake_llm.invoke = AsyncMock(return_value=Ok(_FakeResponse("ok")))
        role = _NoopRole(llm=fake_llm)  # type: ignore[arg-type]
        await role._invoke("sys", "user", key="k2", think=False)
        kwargs = fake_llm.invoke.await_args.kwargs
        assert kwargs["think"] is False

    async def test_invoke_propagates_err(self):
        """If the LLM returns `Err`, `_invoke` returns
        the same `Err` (no implicit parsing)."""
        from kntgraph.core.result import ToolError
        from unittest.mock import AsyncMock

        fake_llm = AsyncMock()
        fake_llm.invoke = AsyncMock(return_value=Err(ToolError("boom")))
        role = _NoopRole(llm=fake_llm)  # type: ignore[arg-type]
        r = await role._invoke("sys", "user", key="k3")
        assert r.is_err()
        assert "boom" in str(r.err_value())


class _FakeResponse:
    """Minimal stand-in for `LLMResponse` used in
    the `_invoke` tests. The base only needs the
    value to round-trip through `Ok()`.
    """

    def __init__(self, text: str) -> None:
        self.text = text
