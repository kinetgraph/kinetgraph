# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.role_systems._base -- ``_BaseRoleSystem``.

The shared machinery for the ECS-shaped role systems
(:class:`ChatRoleSystem`, :class:`PlannerRoleSystem`,
:class:`SummarizerRoleSystem`,
:class:`PersonalizedRoleSystem`,
:class:`RuleBasedChatSystem`).

Lives in its own module so the rule-based system can
import it without dragging in the legacy role systems
(no circular import).
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel

from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.result import Err, Ok, Result, ToolError
from kntgraph.core.world import World
from kntgraph.tools.system import ToolAwareSystem

from ._prompts import ChatReply, parse_role_output

if TYPE_CHECKING:
    from kntgraph.core.world import AgentView


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
        self._pending_inputs: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._last_seen_event_id: dict[str, str] = {}

    # -- request phase --

    def _build_system_prompt(self) -> str:
        return ""

    def _build_user_prompt(
        self, view, session: SessionComponent | None, new_input: str
    ) -> str:
        return new_input

    def _is_request_event(self, view, event_id: str | None) -> bool:
        if event_id is None:
            return False
        if not self.REQUEST_EVENT_TYPE:
            return False
        return self.REQUEST_EVENT_TYPE in view.components

    def _read_new_input(self, view) -> str:
        data = view.components.get(self.REQUEST_EVENT_TYPE, {})
        if isinstance(data, dict):
            for k in ("message", "task", "text", "input"):
                v = data.get(k)
                if isinstance(v, str):
                    return v
            return str(data)
        return ""

    # -- completion phase --

    def _parse_completion(self, text: str) -> Result[BaseModel, ToolError]:
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
            is_new_event = self._is_new_event(agent_id, last_eid)

            session = view.components.get(SessionComponent)
            if session is None and self.REQUEST_EVENT_TYPE == "user.intent":
                continue

            if is_new_event and self._is_request_event(view, last_eid):
                request_event = self._build_request_event(agent_id, view, session)
                if request_event is not None:
                    events.append(request_event)
                continue

            events.extend(self._consume_pending_completions(agent_id, view))
        return events

    # -- internals --

    def _is_new_event(self, agent_id: str, last_eid: Any) -> bool:
        previous = self._last_seen_event_id.get(agent_id)
        is_new = previous != last_eid
        if is_new and last_eid:
            self._last_seen_event_id[agent_id] = last_eid
        return is_new

    def _build_request_event(
        self,
        agent_id: str,
        view,
        session: SessionComponent | None,
    ) -> Event | None:
        new_input = self._read_new_input(view)
        if not new_input:
            return None
        return self._emit_request(agent_id, view, session, new_input)

    def _consume_pending_completions(self, agent_id: str, view) -> list[Event]:
        events: list[Event] = []
        for rid, comp in self._consume_completion(view):
            pending_input = self._pending_inputs.pop(rid, None)
            pending_agent = self._pending_agents.pop(rid, agent_id)
            if pending_input is None:
                continue
            events.append(
                self._build_completion_event(rid, comp, pending_input, pending_agent)
            )
        return events

    def _build_completion_event(
        self,
        rid: str,
        comp: Any,
        pending_input: str,
        pending_agent: str,
    ) -> Event:
        parsed = self._parse_completion((comp.result or {}).get("text", ""))
        if parsed.is_err():
            return Event.create(
                event_type=f"{self.GENERATED_EVENT_TYPE}.failed",
                agent_id=pending_agent,
                event_class="domain",
                data={
                    "request_event_id": rid,
                    "error": str(parsed.err_value_or_raise()),
                },
                causation_id=UUID(rid),
                correlation=CorrelationContext.new(),
            )
        return Event.create(
            event_type=self.GENERATED_EVENT_TYPE,
            agent_id=pending_agent,
            event_class="domain",
            data={
                "request_event_id": rid,
                "output": parsed.unwrap().model_dump(),
                "input": pending_input,
            },
            causation_id=UUID(rid),
            correlation=CorrelationContext.new(),
        )

    def _emit_request(
        self,
        agent_id: str,
        view,
        session: SessionComponent | None,
        new_input: str,
    ) -> Event | None:
        user_prompt = self._build_user_prompt(view, session, new_input)
        system = self._build_system_prompt()
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


def _emit_chat_completion(
    *,
    base: _BaseRoleSystem,
    view: "AgentView",
    text: str,
    follow_up_questions: list[str],
    input_text: str,
) -> "Event":
    """
    Build a ``chat.reply.generated`` domain event with
    the standard wire format used by the LLM-backed
    chat systems.

    Shared between :class:`ChatRoleSystem` (the LLM
    path) and :class:`RuleBasedChatSystem` (the no-LLM
    path; ADR-049). Both paths produce events with the
    same ``data`` shape (``output`` is a
    ``ChatReply.model_dump()``) so downstream consumers
    (the session recorder, the projection) cannot tell
    the difference.
    """
    output_model = base.OUTPUT_MODEL
    if output_model is ChatReply:
        output_payload: dict[str, Any] = {
            "reply": text,
            "follow_up_questions": list(follow_up_questions),
        }
    else:
        output_payload = {"text": text}
    rid = view.last_event_id
    return Event.create(
        event_type=base.GENERATED_EVENT_TYPE,
        agent_id=view.agent_id,
        event_class="domain",
        correlation=CorrelationContext(
            correlation_id=UUID(str(rid)) if rid else UUID(int=0)
        ),
        causation_id=UUID(str(rid)) if rid else None,
        data={
            "request_event_id": str(rid) if rid else "",
            "output": output_payload,
            "input": input_text,
        },
    )
