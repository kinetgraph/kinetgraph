# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
SummarizerRole — semantic specialization: summarize text.

A `Role` is NOT a Tool. It uses a `LiteLLMTool` injected via
constructor. It knows the domain prompt and the output schema.

Why separate from the Tool? See ADR-006. Short version:
- A Tool = 1 capability of I/O.
- A Role = N specializations of "what to ask the LLM".

Multiple roles share the same `LiteLLMTool` instance and
share its rate limit, cost budget, and fallback chain.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from kntgraph.core.result import Err, Result, ToolError

from ..tools.llm import LiteLLMTool
from ._base import _BaseLLMRole


# -----------------------------------------------------------------------------
# Output schema
# -----------------------------------------------------------------------------


class Summary(BaseModel):
    """Typed output of the SummarizerRole."""

    summary: str = Field(..., description="Concise summary, 2-3 sentences")
    key_points: list[str] = Field(
        default_factory=list,
        description="Bullet points of main ideas",
    )
    word_count: int = Field(..., description="Word count of the summary")


# -----------------------------------------------------------------------------
# Role
# -----------------------------------------------------------------------------


class SummarizerRole(_BaseLLMRole):
    """
    Summarizer role: text → Summary.

    Usage:

        llm = LiteLLMTool(default_model="gpt-4o-mini")
        summarizer = SummarizerRole(llm=llm)
        result = await summarizer.summarize(
            text="Long document...",
            max_words=100,
        )
        if result.is_ok():
            summary: Summary = result.unwrap()
            print(summary.summary)
    """

    DEFAULT_MAX_TOKENS = 512
    DEFAULT_TEMPERATURE = 0.0
    OUTPUT_PREFIX = "summary"

    SYSTEM_PROMPT = """\
You are a precise summarizer. Given a text, produce:
  - `summary`: a concise 2-3 sentence summary
  - `key_points`: 3-7 bullet points of the main ideas
  - `word_count`: word count of `summary` (integer)

Respond ONLY with valid JSON matching this schema:
{"summary": str, "key_points": [str, ...], "word_count": int}

Do not include any prose outside the JSON.
"""

    def __init__(
        self,
        llm: LiteLLMTool,
        *,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        super().__init__(
            llm,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def summarize(
        self,
        text: str,
        *,
        max_words: int = 100,
        idempotency_key: Optional[str] = None,
        **invoke_kwargs: Any,
    ) -> Result[Summary, ToolError]:
        """
        Summarize `text` into a `Summary`.

        `idempotency_key` defaults to a stable hash of
        (text, max_words). Same input → same key → safe to
        cache or dedupe.

        Extra `**invoke_kwargs` are forwarded to
        `LiteLLMTool.invoke` (e.g. `think=False` for
        thinking Ollama models).
        """
        if (err := self._check_input(text, "text")) is not None:
            return err

        key = idempotency_key or self._stable_key(max_words, text)
        user_prompt = (
            f"Summarize the following text in at most {max_words} words:\n\n{text}"
        )

        r = await self._invoke(
            self.SYSTEM_PROMPT, user_prompt, key=key, **invoke_kwargs
        )
        if r.is_err():
            return Err(r.err_value_or_raise())
        return self._parse_json(r.unwrap().text, Summary, "summary_parse_error")
