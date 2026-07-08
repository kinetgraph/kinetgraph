# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Base class for Roles that use a `LiteLLMTool`.

A `Role` is a *semantic* specialization layered on top of
a generic LLM completion Tool. It knows the system prompt
for its domain, the output schema, and how to shape the
input (history, context, profile).

Across `ChatRole`, `PlannerRole`, `SummarizerRole`, and
`PersonalizedRole`, the same four steps repeat:

  1. Validate the input is non-empty.
  2. Build the idempotency key (stable hash of inputs).
  3. Call `LiteLLMTool.invoke(...)` with the role's
     system prompt, user prompt, and config.
  4. Parse the JSON response into the role's output model
     (or pass through the raw text for free-form roles
     like `PersonalizedRole`).

`_BaseLLMRole` factors out (1)-(3); (4) lives in a small
helper because some roles don't parse at all.

Why not a Protocol? The four roles share implementation,
not just interface. A base class lets the common path
evolve in one place (e.g. when adding a metrics hook,
the next-model fallback, or a retry budget).
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from kntgraph.core.result import Err, Ok, Result, ToolError
from pydantic import BaseModel, ValidationError

from ..tools.llm import LiteLLMTool
from ._parsing import parse_model_json


class _BaseLLMRole:
    """
    Shared implementation for roles backed by `LiteLLMTool`.

    Subclasses:
      - Set class-level `DEFAULT_MAX_TOKENS` /
        `DEFAULT_TEMPERATURE` to override the constructor
        defaults.
      - Define `SYSTEM_PROMPT` (str) for the role.
      - Define `OUTPUT_PREFIX` (str) used by `_stable_key`
        (e.g. "chat", "plan", "summary", "personalized").
      - Call `_check_input(value, kind)` to validate
        non-empty input.
      - Call `_invoke(system, user, *, key)` to run the
        LLM (returns the raw `Result[LLMResponse, ToolError]`,
        not yet parsed).
      - Call `_parse_json(text, model_cls, error_prefix)` to
        parse the JSON output into a typed model.

    Subclasses that don't parse (e.g. `PersonalizedRole`)
    use `_invoke` directly and unwrap `.text`.
    """

    DEFAULT_MAX_TOKENS: int = 512
    DEFAULT_TEMPERATURE: float = 0.0
    SYSTEM_PROMPT: str = ""
    OUTPUT_PREFIX: str = "role"

    def __init__(
        self,
        llm: LiteLLMTool,
        *,
        model: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> None:
        self._llm = llm
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    @staticmethod
    def _check_input(value: str, kind: str) -> Optional[Result[Any, ToolError]]:
        """
        Return `Err(ToolError("empty {kind}"))` if `value`
        is missing or whitespace-only; else `None`.

        Caller pattern:

            err = self._check_input(value, "task")
            if err is not None:
                return err
        """
        if not value or not value.strip():
            return Err(ToolError(f"empty {kind}"))
        return None

    @classmethod
    def _stable_key(cls, *parts: Any) -> str:
        """
        Stable idempotency key for the role's input.

        Hashes `prefix|part1|part2|...` (string-coerced,
        pipe-separated) and truncates to 32 hex chars.
        Same inputs always produce the same key, so
        callers can dedupe / cache safely.
        """
        h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
        return f"{cls.OUTPUT_PREFIX}:{h[:32]}"

    async def _invoke(
        self,
        system: str,
        user: str,
        *,
        key: str,
        **invoke_kwargs: Any,
    ) -> Result[Any, ToolError]:
        """
        Call the LLM with the role's standard kwargs
        (`model`, `temperature`, `max_tokens`).
        `**invoke_kwargs` are forwarded to
        `LiteLLMTool.invoke` (e.g. `think=False` for
        thinking Ollama models).

        Returns the raw `Result[LLMResponse, ToolError]`
        from the tool — the caller decides whether to
        parse the JSON output or pass it through.
        """
        return await self._llm.invoke(
            idempotency_key=key,
            system=system,
            user=user,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            **invoke_kwargs,
        )

    @staticmethod
    def _parse_json(
        text: str,
        model_cls: type[BaseModel],
        error_prefix: str,
    ) -> Result[BaseModel, ToolError]:
        """
        Parse `text` as the role's output model.

        Returns `Ok(model)` on success or
        `Err(ToolError(f"{error_prefix}: {e!r}"))` on
        `ValidationError` / `ValueError` (which
        `parse_model_json` raises when all parsing
        attempts fail).
        """
        try:
            return Ok(parse_model_json(text, model_cls))
        except (ValidationError, ValueError) as e:
            return Err(ToolError(f"{error_prefix}: {e!r}"))
