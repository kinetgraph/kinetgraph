# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
PersonalizedRole — semantic specialization: produce a
response that adapts to a user's profile preferences.

A `Role` is NOT a Tool. It uses a `LiteLLMTool` injected via
constructor. It reads a `ProfileState` and shapes the
output (language, tone, verbosity) accordingly.

This Role is a *wrapper* — it composes another Role to
generate the content, then re-asks the LLM to rephrase if
the profile's language doesn't match the response. For
simple cases, it just injects a system-prompt prefix
and delegates.

For now: takes a task + profile → output text shaped per
profile. Used by `examples/06_profile_preferences.py`.
"""

from __future__ import annotations

from typing import Any, Optional

from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.memory.profile import ProfileState

from ..tools.llm import LiteLLMTool
from ._base import _BaseLLMRole


# Mapping of supported language codes → instruction text.
# Profiles use these codes (e.g. "pt-BR", "en", "es").
_LANG_INSTRUCTIONS: dict[str, str] = {
    "pt-BR": "Responda em português brasileiro.",
    "pt": "Responda em português.",
    "en": "Respond in English.",
    "es": "Responde en español.",
}

# Mapping of tone preference → style instruction.
_TONE_INSTRUCTIONS: dict[str, str] = {
    "formal": "Use formal, professional language.",
    "casual": "Use a casual, friendly tone.",
    "concise": "Be brief and to the point.",
    "verbose": "Be thorough and detailed.",
}


class PersonalizedRole(_BaseLLMRole):
    """
    Wraps the LiteLLMTool with profile-aware behavior.

    Reads language/tone preferences from the profile and
    builds a system-prompt prefix that conditions the LLM
    output accordingly.
    """

    DEFAULT_MAX_TOKENS = 512
    DEFAULT_TEMPERATURE = 0.3
    OUTPUT_PREFIX = "personalized"

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

    async def respond(
        self,
        profile: ProfileState,
        task: str,
        *,
        idempotency_key: Optional[str] = None,
        **invoke_kwargs: Any,
    ) -> Result[str, ToolError]:
        """
        Produce a response to `task` shaped by `profile`.

        The response is the raw text from the LLM, with
        no structured parsing (this Role is lighter-weight
        than Summarizer/Planner; it just adds a personalized
        system prompt).

        Extra `**invoke_kwargs` are forwarded to
        `LiteLLMTool.invoke` (e.g. `think=False` for
        thinking Ollama models).
        """
        if (err := self._check_input(task, "task")) is not None:
            return err

        system = self._build_system(profile)
        key = idempotency_key or self._stable_key(
            profile.tier,
            "|".join(f"{k}={v}" for k, v in sorted(profile.preferences.items())),
            task,
        )

        r = await self._invoke(system, task, key=key, **invoke_kwargs)
        if r.is_err():
            return Err(r.err_value_or_raise())
        return Ok(r.unwrap().text)

    def _build_system(self, profile: ProfileState) -> str:
        parts: list[str] = [
            "You are a helpful assistant. Adapt your "
            "response to the user's profile preferences.",
        ]
        lang = profile.preferences.get("language")
        if lang and lang in _LANG_INSTRUCTIONS:
            parts.append(_LANG_INSTRUCTIONS[lang])
        tone = profile.preferences.get("tone")
        if tone and tone in _TONE_INSTRUCTIONS:
            parts.append(_TONE_INSTRUCTIONS[tone])
        verbosity = profile.preferences.get("verbosity")
        if verbosity == "low":
            parts.append("Keep the response under 80 words.")
        elif verbosity == "high":
            parts.append("Provide a detailed response with examples.")
        return "\n".join(parts)
