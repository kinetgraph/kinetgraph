# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Prompt engineering + output schemas for the role systems.

This module centralises the artefacts that were previously
embedded in the legacy ``ChatRole`` / ``PlannerRole`` /
``SummarizerRole`` / ``PersonalizedRole`` classes
(``SYSTEM_PROMPT`` constants, the Pydantic output
schemas, the history-formatter helper). The role
systems (``ChatRoleSystem`` / ``PlannerRoleSystem``
/ etc) in ``kntgraph.agents.role_systems`` import
from here so the prompt engineering lives in one
place and the legacy ``kntgraph.agents.roles``
package can be removed (ADR-039 + ADR-043 follow-up:
the legacy roles were the synchronous wrapper path
around ``LiteLLMTool``; the ECS systems are the
canonical path post-v0.9).
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class ChatReply(BaseModel):
    """The assistant's reply to a chat turn.

    ``follow_up_questions`` is an optional list of 0-3
    questions the user might naturally ask next
    (empty list if none). The LLM is prompted to
    produce this list; the downstream system is free
    to ignore it (e.g. the legacy
    ``SessionChatSystem`` in example 05b does not
    surface them).
    """

    reply: str
    follow_up_questions: list[str] = []


CHAT_SYSTEM_PROMPT = """\
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


def format_chat_history(
    *,
    session_id: str,
    user_id: str,
    tenant_id: str,
    messages: list[dict],
    new_message: str,
) -> str:
    """Format a chat session as a transcript for the LLM.

    The session is rendered as ``role: content`` lines;
    the new user message is appended under a "## New
    user message" header. The function is pure (no I/O)
    so the ``ChatRoleSystem`` can call it from the
    request phase.
    """
    lines: list[str] = [
        f"# Conversation in session {session_id}",
        f"user_id: {user_id}",
        f"tenant_id: {tenant_id}",
        "",
        "## History",
    ]
    for m in messages:
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


def chat_idempotency_key(
    *,
    session_id: str,
    history_length: int,
    new_message: str,
) -> str:
    """Stable cache key for a chat turn.

    The key includes the session id, the history
    length (so a different conversation shape
    produces a different key), and the new user
    message. The caller can pass an
    ``idempotency_key=`` kwarg to override the
    default (the legacy role accepted this).
    """
    h = hashlib.sha256(
        f"{session_id}|{history_length}|{new_message}".encode("utf-8")
    ).hexdigest()
    return f"chat:{h[:32]}"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class PlanStep(BaseModel):
    """A single step in a ``Plan``."""

    name: str
    description: str
    depends_on: list[str] = []


class Plan(BaseModel):
    """A planner's decomposition of a task into ordered steps."""

    goal: str
    steps: list[PlanStep]
    rationale: str
    risks: list[str] = []


PLANNER_SYSTEM_PROMPT = """\
You are a precise planner. Given a task, produce:
  - `goal`: a one-sentence restatement of the task
  - `steps`: a list of PlanStep, each with:
      - `name`: short verb-phrase (e.g. "validate_cnpj")
      - `description`: what this step does
      - `depends_on`: list of step names that must precede
  - `rationale`: why this ordering makes sense
  - `risks`: list of strings (open questions, things to verify)

Respond ONLY with valid JSON matching this schema:
{
  "goal": str,
  "steps": [{"name": str, "description": str, "depends_on": [str, ...]}],
  "rationale": str,
  "risks": [str, ...]
}

Do not include any prose outside the JSON.
"""


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------


class Summary(BaseModel):
    """A summariser's output: a short summary + key points."""

    summary: str
    key_points: list[str]
    word_count: int


SUMMARIZER_SYSTEM_PROMPT = """\
You are a precise summarizer. Given a text, produce:
  - `summary`: a concise 2-3 sentence summary
  - `key_points`: 3-7 bullet points of the main ideas
  - `word_count`: word count of `summary` (integer)

Respond ONLY with valid JSON matching this schema:
{"summary": str, "key_points": [str, ...], "word_count": int}

Do not include any prose outside the JSON.
"""


# ---------------------------------------------------------------------------
# Personalized
# ---------------------------------------------------------------------------


PERSONALIZED_SYSTEM_PROMPT = """\
You are a helpful assistant. Adapt your response to the
user's profile preferences.
"""


_LANG_INSTRUCTIONS: dict[str, str] = {
    "en": "Respond in English.",
    "pt-BR": "Responda em português brasileiro.",
    "es": "Responde en español.",
}


_TONE_INSTRUCTIONS: dict[str, str] = {
    "formal": "Use a formal, professional tone.",
    "casual": "Use a casual, friendly tone.",
}


def build_personalized_system_prompt(
    preferences: dict[str, str],
) -> str:
    """Build a profile-aware system prompt.

    ``preferences`` is a flat dict; the keys
    ``language`` / ``tone`` / ``verbosity`` are
    honoured (matching the legacy role's contract).
    Other keys are ignored.
    """
    parts: list[str] = [PERSONALIZED_SYSTEM_PROMPT]
    lang = preferences.get("language")
    if lang and lang in _LANG_INSTRUCTIONS:
        parts.append(_LANG_INSTRUCTIONS[lang])
    tone = preferences.get("tone")
    if tone and tone in _TONE_INSTRUCTIONS:
        parts.append(_TONE_INSTRUCTIONS[tone])
    verbosity = preferences.get("verbosity")
    if verbosity == "low":
        parts.append("Keep the response under 80 words.")
    elif verbosity == "high":
        parts.append("Provide a detailed response with examples.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def parse_role_output(text: str, model: type[BaseModel]) -> BaseModel:
    """Parse the LLM's JSON reply into ``model``.

    Strips the leading/trailing whitespace; tolerates
    code fences (the legacy role did this; the new
    role systems inherit the same contract).
    """
    import json
    import re

    cleaned = text.strip()
    # Strip a leading ```json ... ``` fence if
    # present.
    fence_match = re.match(
        r"^```(?:json)?\s*\n?(.*?)\n?```$",
        cleaned,
        flags=re.DOTALL,
    )
    if fence_match is not None:
        cleaned = fence_match.group(1).strip()
    return model.model_validate(json.loads(cleaned))


__all__ = [
    "CHAT_SYSTEM_PROMPT",
    "PLANNER_SYSTEM_PROMPT",
    "SUMMARIZER_SYSTEM_PROMPT",
    "PERSONALIZED_SYSTEM_PROMPT",
    "ChatReply",
    "Plan",
    "PlanStep",
    "Summary",
    "format_chat_history",
    "chat_idempotency_key",
    "build_personalized_system_prompt",
    "parse_role_output",
]
