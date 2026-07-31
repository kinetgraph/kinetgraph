# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
ECS-shaped systems for the legacy Roles (ADR-039 + ADR-043).

The legacy ``ChatRole``, ``PlannerRole``, ``SummarizerRole``,
and ``PersonalizedRole`` are **synchronous orchestrators**:
the caller ``await role.reply(session, msg)`` and the role
runs the LLM in the same async context. This pattern blocks
the dispatcher's event loop for the duration of the LLM
call (0.3-0.5s with Ollama, more with hosted providers).

The ECS-shaped systems in this module are the **migration
path** (ADR-039 / ADR-043 / ADR-044 follow-up): each role
is a ``WorldSystem`` (a pure ``__call__(world) -> list[Event]``)
that:

  1. Detects a new domain event (``user.intent`` /
     ``plan.request`` / ``summary.request`` /
     ``personalized.request``).
  2. Reads the ``SessionComponent`` (or equivalent) from
     the ``AgentView``.
  3. Emits a ``tool.chat_llm.requested`` event with the
     role's ``SYSTEM_PROMPT`` and the role's input
     formatting. The ``WorkerManager`` runs the LLM in a
     separate process (ADR-043).
  4. When the ``tool.chat_llm.completed`` event lands in
     a subsequent tick, parses the JSON response into the
     role's typed output and emits a domain event
     (``chat.reply.generated`` / ``plan.generated`` /
     ``summary.generated`` / ``personalized.reply.generated``).

The systems REUSE the legacy role's ``SYSTEM_PROMPT`` and
input-formatting helpers (``_format_history`` / equivalent)
so the migration is a thin port: the prompt engineering
and the output schema stay in one place, and the
synchronous ``await role.reply()`` becomes an event-driven
``system(world)`` cycle.

The dispatcher's event loop is NOT blocked while the LLM
runs; the system emits the request and returns immediately
(``events = system(world)`` returns a list with the
``tool.chat_llm.requested`` event, then the system is
inert until the next tick when the completion arrives).

See ``examples/05c_session_chat_ecs_roles.py`` for the
end-to-end reference (the canonical migration of
``ChatRole``).

Migration cheat-sheet:

    # Legacy (deprecated v0.8.0, removed v1.0.0):
    chat = ChatRole(llm=llm, persona="...")
    r = await chat.reply(session, new_user_message)
    reply: ChatReply = r.unwrap()

    # New (this module):
    system = ChatRoleSystem(persona="...")
    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[system],
        ...
    )
    # Emit a ``user.intent`` event; the system handles
    # the rest. The ``chat.reply.generated`` event lands
    # in a later tick with the typed ``ChatReply`` payload.

ADR-049 adds :class:`RuleBasedChatSystem`, the no-LLM
path that short-circuits ``user.intent`` events with
deterministic responses (Zero Token Architecture).
"""

from __future__ import annotations

from pydantic import BaseModel

from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.result import Ok, Result, ToolError

from ._base import _BaseRoleSystem
from ._prompts import (
    CHAT_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    SUMMARIZER_SYSTEM_PROMPT,
    ChatReply,
    Plan,
    Summary,
    build_personalized_system_prompt,
    format_chat_history,
)
from ._rule_based import ChatRule, RuleBasedChatSystem


__all__ = [
    "ChatRoleSystem",
    "ChatRule",
    "PlannerRoleSystem",
    "RuleBasedChatSystem",
    "SummarizerRoleSystem",
    "PersonalizedRoleSystem",
]


# Domain event types emitted by the role systems when the
# LLM reply has been parsed into a typed model.
EVENT_TYPE_CHAT_REPLY_GENERATED = "chat.reply.generated"
EVENT_TYPE_PLAN_GENERATED = "plan.generated"
EVENT_TYPE_SUMMARY_GENERATED = "summary.generated"
EVENT_TYPE_PERSONALIZED_REPLY_GENERATED = "personalized.reply.generated"


# The user-intent event types each role system reacts to.
EVENT_TYPE_USER_INTENT = "user.intent"
EVENT_TYPE_PLAN_REQUEST = "plan.request"
EVENT_TYPE_SUMMARY_REQUEST = "summary.request"
EVENT_TYPE_PERSONALIZED_REQUEST = "personalized.request"


# ---------------------------------------------------------------------------
# ChatRoleSystem
# ---------------------------------------------------------------------------


class ChatRoleSystem(_BaseRoleSystem):
    """
    ECS-shaped ``ChatRole`` (ADR-039 + ADR-043 + ADR-044).
    """

    REQUEST_EVENT_TYPE = EVENT_TYPE_USER_INTENT
    GENERATED_EVENT_TYPE = EVENT_TYPE_CHAT_REPLY_GENERATED
    OUTPUT_MODEL = ChatReply

    def __init__(self, *, persona: str = "") -> None:
        super().__init__()
        self._persona = persona

    def _build_system_prompt(self) -> str:
        if self._persona:
            return f"{self._persona}\n\n{CHAT_SYSTEM_PROMPT}"
        return CHAT_SYSTEM_PROMPT

    def _build_user_prompt(
        self, view, session: SessionComponent | None, new_input: str
    ) -> str:
        if session is None:
            return new_input
        return format_chat_history(
            session_id=session.session_id,
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            messages=list(session.messages),
            new_message=new_input,
        )


# ---------------------------------------------------------------------------
# PlannerRoleSystem
# ---------------------------------------------------------------------------


class PlannerRoleSystem(_BaseRoleSystem):
    """
    ECS-shaped ``PlannerRole``.
    """

    REQUEST_EVENT_TYPE = EVENT_TYPE_PLAN_REQUEST
    GENERATED_EVENT_TYPE = EVENT_TYPE_PLAN_GENERATED
    OUTPUT_MODEL = Plan

    def __init__(self) -> None:
        super().__init__()

    def _build_system_prompt(self) -> str:
        return PLANNER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# SummarizerRoleSystem
# ---------------------------------------------------------------------------


class SummarizerRoleSystem(_BaseRoleSystem):
    """
    ECS-shaped ``SummarizerRole``.
    """

    REQUEST_EVENT_TYPE = EVENT_TYPE_SUMMARY_REQUEST
    GENERATED_EVENT_TYPE = EVENT_TYPE_SUMMARY_GENERATED
    OUTPUT_MODEL = Summary

    def __init__(self) -> None:
        super().__init__()

    def _build_system_prompt(self) -> str:
        return SUMMARIZER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# PersonalizedRoleSystem
# ---------------------------------------------------------------------------


class PersonalizedRoleSystem(_BaseRoleSystem):
    """
    ECS-shaped ``PersonalizedRole``.
    """

    REQUEST_EVENT_TYPE = EVENT_TYPE_PERSONALIZED_REQUEST
    GENERATED_EVENT_TYPE = EVENT_TYPE_PERSONALIZED_REPLY_GENERATED
    OUTPUT_MODEL = BaseModel

    def __init__(self) -> None:
        super().__init__()

    def _build_system_prompt(self) -> str:
        return build_personalized_system_prompt(preferences={})

    def _parse_completion(self, text: str) -> Result[BaseModel, ToolError]:
        class _TextReply(BaseModel):
            text: str

        return Ok(_TextReply(text=text))
