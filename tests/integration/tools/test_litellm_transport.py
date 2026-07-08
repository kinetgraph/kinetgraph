# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the `LiteLLMTransportAdapter` exception
translation chain.

These tests exercise the `try/except` chain on real
`litellm.*Error` classes, monkeypatching `litellm.acompletion`
to raise a real `litellm` exception. They therefore require
the `litellm` package at test time (dependency of the
`kntgraph[llm]` extra) and live under `integration/` rather
than `unit/`, which must run with no optional extras installed.

The translation is what makes the fallback loop in
`LiteLLMTool.invoke()` work in production — without it, real
`RateLimitError` from the provider would fall into the
generic `except Exception` branch and the tool would not try
the next model in `fallback_models`.

Originally lived in `tests/agents/unit/tools/test_llm.py`;
moved here so `uv run scripts/ci.py` (which only runs the
unit-test suite) does not require the `llm` extra.
"""

from __future__ import annotations

import pytest

from kntgraph.agents.tools.llm import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LiteLLMTransportAdapter,
    LiteLLMTool,
)
from kntgraph.tools.llm_transport import LLMRequest


class TestLiteLLMTransportAdapterExceptionTranslation:
    """
    `LiteLLMTransportAdapter.complete()` catches the three
    `litellm` error families and re-raises them as
    `LLMRateLimitError`, `LLMAuthError`, `LLMError`
    subclasses. We patch `litellm.acompletion` to raise the
    native exception and assert the typed exception is raised.
    """

    async def test_rate_limit_translates_to_typed(self):
        import litellm

        transport = LiteLLMTransportAdapter()

        async def _raise_429(**kwargs):
            raise litellm.RateLimitError(
                message="rate limited",
                model="gpt-x",
                llm_provider="openai",
            )

        # Patch the `acompletion` symbol the transport
        # already imported.
        original = litellm.acompletion
        litellm.acompletion = _raise_429
        try:
            with pytest.raises(LLMRateLimitError):
                await transport(
                    LLMRequest(
                        model="gpt-x",
                        messages=[{"role": "user", "content": "u"}],
                        temperature=0.5,
                        max_tokens=10,
                        extra={},
                    )
                )
        finally:
            litellm.acompletion = original

    async def test_auth_translates_to_typed(self):
        import litellm

        transport = LiteLLMTransportAdapter()

        async def _raise_401(**kwargs):
            raise litellm.AuthenticationError(
                message="invalid api key",
                model="gpt-x",
                llm_provider="openai",
            )

        original = litellm.acompletion
        litellm.acompletion = _raise_401
        try:
            with pytest.raises(LLMAuthError):
                await transport(
                    LLMRequest(
                        model="gpt-x",
                        messages=[{"role": "user", "content": "u"}],
                        temperature=0.5,
                        max_tokens=10,
                        extra={},
                    )
                )
        finally:
            litellm.acompletion = original

    async def test_generic_api_error_translates_to_typed(self):
        import litellm

        transport = LiteLLMTransportAdapter()

        async def _raise_500(**kwargs):
            raise litellm.APIError(
                status_code=500,
                message="server error",
                model="gpt-x",
                llm_provider="openai",
            )

        original = litellm.acompletion
        litellm.acompletion = _raise_500
        try:
            with pytest.raises(LLMError) as exc_info:
                await transport(
                    LLMRequest(
                        model="gpt-x",
                        messages=[{"role": "user", "content": "u"}],
                        temperature=0.5,
                        max_tokens=10,
                        extra={},
                    )
                )
            # Must be the LLMError base, not a
            # LLMRateLimitError / LLMAuthError subclass.
            assert type(exc_info.value) is LLMError
        finally:
            litellm.acompletion = original

    async def test_real_rate_limit_triggers_fallback_chain(self):
        """
        End-to-end: the fallback chain in
        `LiteLLMTool.invoke()` must work against the
        *real* `LiteLLMTransportAdapter` (not the fake). When
        the provider raises `litellm.RateLimitError`,
        the tool translates it to `LLMRateLimitError`
        and tries the next model in `fallback_models`.
        """
        import litellm

        call_count = {"n": 0}

        async def _first_429_then_ok(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise litellm.RateLimitError(
                    message="rate limited",
                    model=kwargs["model"],
                    llm_provider="openai",
                )
            return {
                "model": kwargs["model"],
                "choices": [
                    {
                        "message": {"content": "ok", "role": "assistant"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        original = litellm.acompletion
        litellm.acompletion = _first_429_then_ok
        try:
            tool = LiteLLMTool(
                default_model="primary",
                fallback_models=["fallback"],
                transport=LiteLLMTransportAdapter(),
            )
            r = await tool.invoke(idempotency_key="k", system="s", user="u")
            assert r.is_ok()
            assert r.ok_value().text == "ok"
            # The fallback chain fired: primary
            # raised, fallback succeeded.
            assert call_count["n"] == 2
        finally:
            litellm.acompletion = original
