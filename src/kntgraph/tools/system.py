# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Helper for building WorldSystems that request tool executions via the Worker Pattern.
"""

from __future__ import annotations

from typing import Mapping, Optional, cast
from uuid import UUID

from kntgraph.core._typing import JsonValue
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world.components import ToolCallCompletion, ToolCallRequest
from kntgraph.core.world.view import AgentView


class ToolAwareSystem:
    """
    Mixin/Helper for Systems that need to interact with external tools.
    Provides methods to check tool call state in an AgentView and emit
    ``tool.<name>.requested`` events (the canonical ADR-036 form;
    see ``request_tool``).
    """

    def request_tool(
        self,
        agent_id: str,
        tool_name: str,
        params: Mapping[str, JsonValue],
        causation_id: Optional[str] = None,
        correlation: Optional[CorrelationContext] = None,
    ) -> Event:
        """
        Builds a ``tool.<name>.requested`` event (the canonical
        ADR-036 form; the older bare ``tool.requested`` form is
        accepted by the projection for back-compat only). The
        system should return this event in its output list.

        ``correlation`` is required (ADR-037). The
        caller (the system itself) MUST pass a
        ``CorrelationContext`` that propagates the
        flow. Typically, the system holds the
        ``CorrelationContext`` from its entry event
        (e.g. via ``correlation_middleware.continue_from(entry)``)
        and re-uses it for every downstream event.
        For the rare case where the system cannot
        determine the correlation, pass
        ``CorrelationContext.new(correlation_id=uuid4())``
        to start a fresh flow.
        """
        if correlation is None:
            raise TypeError(
                "ToolAwareSystem.request_tool requires "
                "'correlation' (ADR-037). Pass a "
                "CorrelationContext (e.g. via "
                "correlation_middleware.continue_from(parent) "
                "or CorrelationContext.new(...))."
            )
        payload: dict[str, JsonValue] = {
            "tool": tool_name,
            "params": dict(params),
        }
        # ``causation_id`` comes in as ``str | None`` (the
        # EventView stores it as a string projection of
        # the underlying UUID). ``Event.create`` expects a
        # ``UUID | None``; the cast is a no-op when the
        # caller already passed a real UUID. Production
        # callers in the EventLog path always pass a
        # stringified UUID.
        return Event.create(
            event_type=f"tool.{tool_name}.requested",
            agent_id=agent_id,
            event_class="domain",
            data=payload,
            causation_id=cast("Optional[UUID]", causation_id),
            correlation=correlation,
        )

    def get_request(
        self, view: AgentView, request_event_id: str
    ) -> Optional[ToolCallRequest]:
        """
        Finds a ToolCallRequest by its ID in the agent's view.
        """
        tool_requests = view.components.get("tool_requests", {})
        return tool_requests.get(request_event_id)

    def get_completion(
        self, view: AgentView, request_event_id: str
    ) -> Optional[ToolCallCompletion]:
        """
        Finds a ToolCallCompletion by the request's ID.
        """
        completions = view.components.get("tool_completions", {})
        return completions.get(request_event_id)

    def has_requested(self, view: AgentView, request_event_id: str) -> bool:
        """
        Check if a specific tool request exists.
        """
        return self.get_request(view, request_event_id) is not None

    def is_pending(self, view: AgentView, request_event_id: str) -> bool:
        """
        Check if a tool request has been made but not yet completed.
        """
        has_req = self.has_requested(view, request_event_id)
        has_comp = self.get_completion(view, request_event_id) is not None
        return has_req and not has_comp
