# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
ChatRole — semantic specialization: produce the next
assistant message in a conversational session.

A `Role` is NOT a Tool. It uses a `LiteLLMTool` injected via
constructor. It knows how to format a `SessionState`'s
message history into a prompt for the LLM and how to
extract the next assistant turn.

Why a Role and not a Tool? Chat is a SEMANTIC concern
(history formatting, persona, response style) layered on
top of a generic LLM completion. The `LiteLLMTool` is the
pluggable I/O; this Role is one possible specialization.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from pydantic import BaseModel, Field

from kntgraph.core.result import Err, Result, ToolError
from kntgraph.memory.session import SessionState

from ..tools.llm import LiteLLMTool
from ._base import _BaseLLMRole


# -----------------------------------------------------------------------------
# Output schema
# -----------------------------------------------------------------------------


class ChatReply(BaseModel):
    """Typed output of the ChatRole."""

    reply: str = Field(..., description="The assistant's next message")
    # Optional: model can hint at follow-up questions,
    # internal notes, etc. Not required, but useful for
    # richer UIs.
    follow_up_questions: list[str] = Field(
        default_factory=list,
        description="Possible next user questions to suggest",
    )


# -----------------------------------------------------------------------------
# Role
# -----------------------------------------------------------------------------


class ChatRole(_BaseLLMRole):
    """
    Conversational role: session history + new user message
    → next assistant reply.

    Usage:

        sm = SessionManager(event_log=log, redis_client=redis)
        await sm.start(session_id="s-1", user_id="u-1", tenant_id="t-1")
        await sm.append_message("s-1", role="user", content="olá")

        chat = ChatRole(llm=llm)
        r = await chat.reply(
            session=await sm.read("s-1"),
            new_user_message="como vai?",
        )
        if r.is_ok():
            await sm.append_message("s-1", role="assistant", content=r.unwrap().reply)
    """

    DEFAULT_MAX_TOKENS = 512
    DEFAULT_TEMPERATURE = 0.7
    OUTPUT_PREFIX = "chat"

    SYSTEM_PROMPT = """\
You are a helpful, friendly assistant. You are continuing
a conversation. Your task is to produce the next assistant
reply given the full message history.

Respond ONLY with valid JSON matching this schema:
{
  "reply": str,
  "follow_up_questions": [str, ...]
}

The `reply` is the message you would send to the user.
The `follow_up_questions` is a list of 0-3 questions the
user might naturally ask next (empty list if none).

Do not include any prose outside the JSON.
"""

    def __init__(
        self,
        llm: LiteLLMTool,
        *,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        persona: str = "",
    ) -> None:
        """
        `persona` is an optional prepended instruction
        (e.g. "You are a tax accountant assistant. Be
        precise and use formal language."). Set per
        deployment.
        """
        super().__init__(
            llm,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        self._persona = persona

    async def reply(
        self,
        session: SessionState,
        new_user_message: str,
        *,
        idempotency_key: Optional[str] = None,
        **invoke_kwargs: Any,
    ) -> Result[ChatReply, ToolError]:
        """
        Generate the next assistant message given the
        session's history and a new user message.

        The session is read-only here — the caller is
        responsible for `append_message` after a successful
        reply.

        Extra `**invoke_kwargs` are forwarded to
        `LiteLLMTool.invoke` (e.g. `think=False` for
        thinking Ollama models).
        """
        if (err := self._check_input(new_user_message, "user message")) is not None:
            return err

        user_prompt = self._format_history(session, new_user_message)
        key = idempotency_key or self._chat_key(session, new_user_message)

        system = self.SYSTEM_PROMPT
        if self._persona:
            system = f"{self._persona}\n\n{system}"

        r = await self._invoke(system, user_prompt, key=key, **invoke_kwargs)
        if r.is_err():
            return Err(r.err_value_or_raise())
        return self._parse_json(  # type: ignore[return-value]
            r.unwrap().text, ChatReply, "chat_parse_error"
        )

    @staticmethod
    def _format_history(session: SessionState, new_message: str) -> str:
        """
        Format the session as a transcript. The session
        already keeps messages in order; we render them
        as `role: content` lines.
        """
        lines: list[str] = [
            f"# Conversation in session {session.session_id}",
            f"user_id: {session.user_id}",
            f"tenant_id: {session.tenant_id}",
            "",
            "## History",
        ]
        for m in session.messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            lines.append(f"{role}: {content}")
        lines.append("")
        lines.append("## New user message")
        lines.append(new_message)
        lines.append("")
        lines.append(
            "Produce the next assistant `reply` as JSON. "
            "Take the conversation context into account."
        )
        return "\n".join(lines)

    @staticmethod
    def _chat_key(session: SessionState, new_message: str) -> str:
        # Chat-specific key: includes the history length so
        # the response changes as the conversation grows.
        # `_BaseLLMRole._stable_key` would be fine too, but
        # we want explicit control over the parts (avoids
        # `user_id`/`tenant_id` coupling the key to the
        # actor — the same conversation shape from a
        # different user should still benefit from cache).
        h = hashlib.sha256(
            f"{session.session_id}|{len(session.messages)}|{new_message}".encode(
                "utf-8"
            )
        ).hexdigest()
        return f"chat:{h[:32]}"
