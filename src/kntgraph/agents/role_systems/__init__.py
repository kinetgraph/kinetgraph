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
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core.world import World
from kntgraph.tools.system import ToolAwareSystem

from ._prompts import (
    CHAT_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    SUMMARIZER_SYSTEM_PROMPT,
    ChatReply,
    Plan,
    Summary,
    build_personalized_system_prompt,
    format_chat_history,
    parse_role_output,
)


__all__ = [
    "ChatRoleSystem",
    "PlannerRoleSystem",
    "SummarizerRoleSystem",
    "PersonalizedRoleSystem",
]


# Domain event types emitted by the role systems when the
# LLM reply has been parsed into a typed model. The
# frameworks' downstream systems (e.g. the
# ``session_recorder`` tool) can subscribe to these.
EVENT_TYPE_CHAT_REPLY_GENERATED = "chat.reply.generated"
EVENT_TYPE_PLAN_GENERATED = "plan.generated"
EVENT_TYPE_SUMMARY_GENERATED = "summary.generated"
EVENT_TYPE_PERSONALIZED_REPLY_GENERATED = "personalized.reply.generated"


# The user-intent event types each role system reacts to.
EVENT_TYPE_USER_INTENT = "user.intent"
EVENT_TYPE_PLAN_REQUEST = "plan.request"
EVENT_TYPE_SUMMARY_REQUEST = "summary.request"
EVENT_TYPE_PERSONALIZED_REQUEST = "personalized.request"


class _BaseRoleSystem(ToolAwareSystem):
    """
    Shared machinery for the role systems.

    A role system has two phases:

      1. **Request phase**: a new domain event lands
         (``user.intent`` / ``plan.request`` / etc.). The
         system emits a ``tool.chat_llm.requested`` event
         with the role's prompt.

      2. **Completion phase**: the LLM responds (in a
         later tick). The system parses the JSON reply
         into the role's typed output and emits the
         domain event (``chat.reply.generated`` etc.).

    The system tracks per-(agent_id, request_event_id) state
    to correlate the completion back to the request
    (the completion's ``causation_id`` is the request's
    ``event_id``; the framework's tool-call overlay
    accumulates both across ticks — see ADR-044).

    The system REUSES the legacy role's
    ``SYSTEM_PROMPT`` and input-formatting helpers so
    the prompt engineering lives in one place.
    """

    TOOL_NAME = "chat_llm"
    REQUEST_EVENT_TYPE: str = ""
    GENERATED_EVENT_TYPE: str = ""
    OUTPUT_MODEL: type[BaseModel] = BaseModel

    def __init__(self) -> None:
        # ``request_event_id`` -> user message (or
        # task / text) that triggered the request.
        # Recovered at completion time because the
        # ``user.intent`` component on the view is
        # replaced by the ``tool.chat_llm.completed``
        # event's payload (last-event-wins; the
        # default domain projection is documented in
        # ``projection._apply_event``).
        self._pending_inputs: dict[str, str] = {}
        # ``request_event_id`` -> the ``agent_id`` the
        # request was emitted for (so the completion
        # phase emits the generated event on the same
        # agent).
        self._pending_agents: dict[str, str] = {}
        # The ``last_event_id`` per agent; used to detect
        # a new domain event landing in this tick.
        self._last_seen_event_id: dict[str, str] = {}

    # -- request phase --

    def _build_system_prompt(self) -> str:
        """Override in subclasses to add persona / locale."""
        return ""

    def _build_user_prompt(
        self, view, session: SessionComponent | None, new_input: str
    ) -> str:
        """Override in subclasses to format the input.

        The default implementation just returns the raw
        ``new_input`` (suitable for the planner /
        summarizer roles, which do not need a session
        transcript). The chat role overrides this to
        format the full message history.
        """
        return new_input

    def _is_request_event(self, view, event_id: str) -> bool:
        """True if the last folded event on the view is
        a domain event this role system reacts to.

        The default rule: the ``view.components`` map
        has a key matching the role's request event
        type (last-event-wins). Subclasses can override
        for richer semantics.
        """
        if not self.REQUEST_EVENT_TYPE:
            return False
        # The default rule is "the request event type
        # is a key in the view's components" — true
        # when the user.intent (or equivalent) was
        # the last domain event folded in this tick.
        # If a tool event landed in the same batch,
        # the components map has a different key
        # (e.g. ``tool.chat_llm.requested``); in that
        # case the request phase is a no-op (the
        # completion phase handles the response).
        return self.REQUEST_EVENT_TYPE in view.components

    def _read_new_input(self, view) -> str:
        """Read the new user input from the view.

        Default: the ``REQUEST_EVENT_TYPE`` component
        on the view. Subclasses can override for richer
        extraction (e.g. ``view.components[REQUEST_EVENT_TYPE]["message"]``).
        """
        data = view.components.get(self.REQUEST_EVENT_TYPE, {})
        if isinstance(data, dict):
            # Common shapes: ``{"message": "..."}``,
            # ``{"task": "..."}``, ``{"text": "..."}``.
            for k in ("message", "task", "text", "input"):
                v = data.get(k)
                if isinstance(v, str):
                    return v
            return str(data)
        return ""

    # -- completion phase --

    def _parse_completion(self, text: str) -> Result[BaseModel, ToolError]:
        """Parse the LLM's JSON reply into the role's
        typed output model. Returns ``Err(ToolError)`` on
        parse failure (so the system can emit a
        ``<role>.generation_failed`` event downstream).
        """
        try:
            return Ok(parse_role_output(text, self.OUTPUT_MODEL))
        except Exception as e:
            return Err(ToolError(f"{self.GENERATED_EVENT_TYPE}_parse_error: {e}"))

    # -- WorldSystem --

    def __call__(self, world: World) -> list[Event]:
        events: list[Event] = []
        for agent_id, view in world.views.items():
            if not isinstance(view.components, dict):
                continue
            last_eid = view.last_event_id
            is_new_event = (
                agent_id not in self._last_seen_event_id
                or self._last_seen_event_id[agent_id] != last_eid
            )
            if is_new_event and last_eid:
                self._last_seen_event_id[agent_id] = last_eid

            session = view.components.get(SessionComponent)
            if session is None and self.REQUEST_EVENT_TYPE == EVENT_TYPE_USER_INTENT:
                # The chat role requires a session.
                continue

            # Request phase: a new request event landed
            # on this view.
            if is_new_event and self._is_request_event(view, last_eid):
                new_input = self._read_new_input(view)
                if not new_input:
                    continue
                e = self._emit_request(agent_id, view, session, new_input)
                if e is not None:
                    events.append(e)
                continue

            # Completion phase: a chat_llm completion
            # landed on this view.
            for completion in self._consume_completion(view):
                rid, comp = completion
                pending_input = self._pending_inputs.pop(rid, None)
                pending_agent = self._pending_agents.pop(rid, agent_id)
                if pending_input is None:
                    # The request was not emitted by
                    # THIS system (e.g. another role
                    # system emitted a chat_llm
                    # request on the same agent).
                    continue
                parsed = self._parse_completion((comp.result or {}).get("text", ""))
                if parsed.is_err():
                    # The completion was received but
                    # the JSON parse failed. We still
                    # emit an event so downstream
                    # systems can react (e.g. a
                    # fallback path).
                    events.append(
                        Event.create(
                            event_type=f"{self.GENERATED_EVENT_TYPE}.failed",
                            agent_id=pending_agent,
                            event_class="domain",
                            data={
                                "request_event_id": rid,
                                "error": str(parsed.err_value_or_raise()),
                            },
                            causation_id=rid,
                            correlation=CorrelationContext.new(),
                        )
                    )
                    continue
                events.append(
                    Event.create(
                        event_type=self.GENERATED_EVENT_TYPE,
                        agent_id=pending_agent,
                        event_class="domain",
                        data={
                            "request_event_id": rid,
                            "output": parsed.unwrap().model_dump(),
                            "input": pending_input,
                        },
                        causation_id=rid,
                        correlation=CorrelationContext.new(),
                    )
                )
        return events

    # -- internals --

    def _emit_request(
        self,
        agent_id: str,
        view,
        session: SessionComponent | None,
        new_input: str,
    ) -> Event | None:
        """Emit the ``tool.chat_llm.requested`` event.

        Captures the new input and the agent_id so the
        completion phase can recover them. Returns the
        event (or ``None`` if the input is empty).
        """
        user_prompt = self._build_user_prompt(view, session, new_input)
        system = self._build_system_prompt()
        # Build the correlation. We use the last folded
        # event_id as the correlation_id so the
        # completion can be joined to the request.
        last_eid = view.last_event_id
        correlation = CorrelationContext(correlation_id=UUID(str(last_eid)))
        e = self.request_tool(
            agent_id=agent_id,
            tool_name=self.TOOL_NAME,
            params={
                "system": system,
                "user": user_prompt,
            },
            causation_id=str(last_eid),
            correlation=correlation,
        )
        self._pending_inputs[str(e.event_id)] = new_input
        self._pending_agents[str(e.event_id)] = agent_id
        return e

    def _consume_completion(self, view) -> list[tuple[str, Any]]:
        """Find chat_llm completions on the view that
        were emitted by THIS system.

        A completion is "ours" if the
        ``request_event_id`` is in
        ``self._pending_agents``. The framework's
        tool-call overlay accumulates both requests and
        completions across ticks (ADR-044), so a
        completion that landed in a previous tick is
        still on the view.
        """
        tool_completions = view.components.get("tool_completions", {})
        if not isinstance(tool_completions, dict):
            return []
        out: list[tuple[str, Any]] = []
        for rid, comp in tool_completions.items():
            if comp.status != "completed":
                continue
            if rid in self._pending_agents:
                out.append((rid, comp))
        return out


# ---------------------------------------------------------------------------
# ChatRoleSystem
# ---------------------------------------------------------------------------


class ChatRoleSystem(_BaseRoleSystem):
    """
    ECS-shaped ``ChatRole`` (ADR-039 + ADR-043 + ADR-044).

    The system reads the ``SessionComponent`` from the
    ``AgentView`` and emits a ``tool.chat_llm.requested``
    event with the role's ``SYSTEM_PROMPT`` and the
    formatted transcript. When the LLM response arrives
    in a subsequent tick, the system parses the JSON
    reply into a ``ChatReply`` and emits a
    ``chat.reply.generated`` event with the typed
    output.

    Usage:

        system = ChatRoleSystem(persona="...")
        dispatcher = ReactiveDispatcher(
            log=log,
            systems=[system],
            ...
        )
        # Emit ``user.intent`` events; the system handles
        # the rest. ``chat.reply.generated`` events
        # land in later ticks.
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

    The system reacts to ``plan.request`` events. The
    event payload is ``{"task": "..."}``; the system
    emits a ``tool.chat_llm.requested`` event and parses
    the LLM's response into a ``Plan`` when the
    completion lands. The typed output is emitted as
    ``plan.generated``.
    """

    REQUEST_EVENT_TYPE = EVENT_TYPE_PLAN_REQUEST
    GENERATED_EVENT_TYPE = EVENT_TYPE_PLAN_GENERATED
    OUTPUT_MODEL = Plan

    def __init__(self) -> None:
        super().__init__()

    def _build_system_prompt(self) -> str:
        return PLANNER_SYSTEM_PROMPT

    def _read_new_input(self, view) -> str:
        # ``plan.request`` carries the task in
        # ``data["task"]``. The default ``_read_new_input``
        # returns the first string-valued key it finds
        # (``message`` / ``task`` / ``text`` / ``input``);
        # ``task`` matches.
        return super()._read_new_input(view)


# ---------------------------------------------------------------------------
# SummarizerRoleSystem
# ---------------------------------------------------------------------------


class SummarizerRoleSystem(_BaseRoleSystem):
    """
    ECS-shaped ``SummarizerRole``.

    The system reacts to ``summary.request`` events. The
    event payload is ``{"text": "..."}``; the system
    emits a ``tool.chat_llm.requested`` event and parses
    the LLM's response into a ``Summary`` when the
    completion lands. The typed output is emitted as
    ``summary.generated``.
    """

    REQUEST_EVENT_TYPE = EVENT_TYPE_SUMMARY_REQUEST
    GENERATED_EVENT_TYPE = EVENT_TYPE_SUMMARY_GENERATED
    OUTPUT_MODEL = Summary

    def __init__(self) -> None:
        super().__init__()

    def _build_system_prompt(self) -> str:
        return SUMMARIZER_SYSTEM_PROMPT

    def _read_new_input(self, view) -> str:
        return super()._read_new_input(view)


# ---------------------------------------------------------------------------
# PersonalizedRoleSystem
# ---------------------------------------------------------------------------


class PersonalizedRoleSystem(_BaseRoleSystem):
    """
    ECS-shaped ``PersonalizedRole``.

    The system reacts to ``personalized.request`` events.
    The event payload is ``{"input": "..."}`` (or
    ``{"task": "..."}``); the system emits a
    ``tool.chat_llm.requested`` event and the LLM's
    response is emitted as ``personalized.reply.generated``
    with the raw text payload (the role is free-form; the
    legacy role does not parse a JSON output).
    """

    REQUEST_EVENT_TYPE = EVENT_TYPE_PERSONALIZED_REQUEST
    GENERATED_EVENT_TYPE = EVENT_TYPE_PERSONALIZED_REPLY_GENERATED
    # The legacy role returns raw text. We wrap
    # the text in a tiny model so the system's
    # output is uniform.
    OUTPUT_MODEL = BaseModel

    def __init__(self) -> None:
        super().__init__()

    def _build_system_prompt(self) -> str:
        # The system prompt is profile-driven; the
        # default (no profile) is a generic
        # personalised-role prompt. A future hook
        # can read the ``ProfileComponent`` from the
        # view and pass ``preferences`` here.
        return build_personalized_system_prompt(preferences={})

    def _read_new_input(self, view) -> str:
        return super()._read_new_input(view)

    def _parse_completion(self, text: str) -> Result[BaseModel, ToolError]:
        # The legacy role returns raw text. We wrap
        # the text in a tiny model so the system's
        # output is uniform.
        class _TextReply(BaseModel):
            text: str

        return Ok(_TextReply(text=text))
