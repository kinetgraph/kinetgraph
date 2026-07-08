# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Test transport: deterministic, in-memory, no network.

`FakeLLMTransport` returns a pre-programmed sequence of
completions. The tests configure it with the JSON text the
role expects, and the role's parser succeeds.

It also supports raising specific errors (rate limit,
auth, generic) on demand, by inserting sentinel strings
into the system prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from kntgraph.tools.llm_transport import LLMRequest

# Make the LLMTransport / exception classes importable from
# the test fakes without a circular import.
sys.path.insert(
    0,
    str(Path(__file__).parent.parent.parent.parent / "kntgraph" / "src"),
)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

# Use the real typed exception classes so `LiteLLMTool`'s
# `except LLMRateLimitError` / `except LLMAuthError` catches
# the fake's errors. The fake also subclasses the legacy
# `_RateLimitLike` / `_AuthLike` markers for backwards
# compatibility with code that hasn't migrated yet — but
# the production path uses the typed hierarchy.
from kntgraph.agents.tools.llm import (  # noqa: E402
    LLMAuthError,
    LLMRateLimitError,
    _AuthLike,
    _RateLimitLike,
)


class _FakeRateLimitError(LLMRateLimitError, _RateLimitLike):
    pass


class _FakeAuthError(LLMAuthError, _AuthLike):
    pass


class FakeLLMTransport:
    """
    Deterministic transport for unit tests.

    Usage:

        transport = FakeLLMTransport()
        transport.queue_response(
            text='{"summary": "ok", "key_points": [], "word_count": 1}'
        )
        tool = LiteLLMTool(default_model="x", transport=transport)
        r = await tool.invoke(idempotency_key="k", system="s", user="u")

    Or, to simulate errors:

        transport.queue_error("rate_limit")
        transport.queue_error("auth")
        transport.queue_error("generic")
    """

    def __init__(self) -> None:
        self._responses: list[dict] = []
        self._errors: list[str] = []
        self.calls: list[dict] = []

    # ---- queueing

    def queue_response(
        self,
        *,
        text: str,
        model: str = "fake-model",
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
        cost_usd: float = 0.0001,
        finish_reason: str = "stop",
    ) -> None:
        self._responses.append(
            {
                "model": model,
                "choices": [
                    {
                        "message": {"content": text, "role": "assistant"},
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
                "_cost_usd": cost_usd,
            }
        )

    def queue_error(self, kind: str) -> None:
        """kind in: 'rate_limit', 'auth', 'generic'."""
        self._errors.append(kind)

    def reset(self) -> None:
        self._responses.clear()
        self._errors.clear()
        self.calls.clear()

    # ---- LLMTransport interface

    async def __call__(self, request: "LLMRequest") -> dict:
        """
        Iter 28 FU 3: the LLMTransport is now
        ``Callable[LLMRequest, dict]``. Tests that
        call the transport directly (outside the
        ``LiteLLMTool``) can use either ``__call__``
        (new) or ``complete`` (legacy).
        """
        return await self.complete(
            model=request.model,
            messages=request.messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response_format=request.response_format,
            drop_unsupported_params=request.drop_unsupported_params,
            idempotency_key=request.idempotency_key,
            **request.extra,
        )

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        response_format: Optional[dict] = None,
        drop_unsupported_params: bool = True,
        **kwargs: Any,
    ) -> dict:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
                **kwargs,
            }
        )

        # Errors first (FIFO)
        if self._errors:
            kind = self._errors.pop(0)
            if kind == "rate_limit":
                raise _FakeRateLimitError("rate limited")
            if kind == "auth":
                raise _FakeAuthError("invalid api key")
            raise RuntimeError(f"fake generic error ({kind})")

        if not self._responses:
            raise RuntimeError("FakeLLMTransport: no responses queued")

        resp = self._responses.pop(0)
        # _cost_usd is internal; LiteLLMTool extracts via
        # litellm.completion_cost which won't see it. We
        # just put it in raw and accept that real cost
        # computation is None in tests. The cost is still
        # recorded in the test's own assertion if needed.
        return resp
