# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kntgraph.core.world import World
from kntgraph.core.world.view import AgentView
from kntgraph.core.event import Event, CorrelationContext
from kntgraph.tools.registry import ToolRegistry
from kntgraph.tools.acl import default_acl
from kntgraph.security import Principal


@dataclass(frozen=True, slots=True)
class RoleComponent:
    """Semantic Role defining the Agent's persona and allowed capabilities."""

    persona: str
    instructions: str
    allowed_tools: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IntentComponent:
    """Execution context Intent containing target tool, parameters, and correlation context."""

    target_tool: str
    parameters: dict[str, Any]
    status: str  # "pending" | "processing" | "completed" | "failed"
    correlation: CorrelationContext


class IntentResolutionSystem:
    """Pure WorldSystem: Evaluates pending intents, validates them against

    Semantic Roles and Security ACLs, and emits `tool.<name>.requested` events.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def __call__(self, world: World) -> list[Event]:
        events: list[Event] = []

        # 1. Native ECS Lazy Query: Only process entities that possess all required components
        for agent_id, view in world.query_agents(
            RoleComponent, IntentComponent, Principal
        ):
            intent = self._get_comp(view, IntentComponent)
            if intent.status != "pending":
                continue

            role = self._get_comp(view, RoleComponent)
            principal = self._get_comp(view, Principal)
            target_tool_name = intent.target_tool

            # 2. Registry Check: Does the tool exist?
            tool = self._registry.get(target_tool_name)
            if not tool:
                events.append(
                    self._fail_event(
                        agent_id,
                        view,
                        f"Tool {target_tool_name} not found.",
                        intent.correlation,
                    )
                )
                continue

            # 3. Physical Security (L1/L2): Does the ToolACL allow this Principal?
            acl = self._registry.acl_for(target_tool_name) or default_acl()
            allowed, reason = acl.check(principal)
            if not allowed:
                events.append(
                    self._fail_event(
                        agent_id,
                        view,
                        f"ACL Denied: {reason}",
                        intent.correlation,
                    )
                )
                continue

            # 4. Semantic Security: Does the Agent's current Role allow this tool?
            if target_tool_name not in role.allowed_tools:
                events.append(
                    self._fail_event(
                        agent_id,
                        view,
                        "Semantic Role not authorized.",
                        intent.correlation,
                    )
                )
                continue

            # 5. Success: Emit the execution request directly (parameters validated by the Tool on execution)
            # Format MUST be tool.<name>.requested for Full Payload Fan-Out (ADR-036)
            # The "tool" key is kept in data for project_tool_calls compatibility (ADR-034)
            events.append(
                Event.domain_from(
                    agent_id=agent_id,
                    type=f"tool.{target_tool_name}.requested",
                    data={
                        "tool": target_tool_name,
                        "params": intent.parameters,
                    },
                    causation_id=view.last_event_id,
                    correlation=intent.correlation,
                )
            )

        return events

    def _fail_event(
        self,
        agent_id: str,
        view: AgentView,
        reason: str,
        correlation: CorrelationContext,
    ) -> Event:
        return Event.domain_from(
            agent_id=agent_id,
            type="intent.validation_failed",
            data={"reason": reason},
            causation_id=view.last_event_id,
            correlation=correlation,
        )

    def _get_comp(self, view: AgentView, comp_type: type) -> Any:
        return next(c for c in view.components.values() if isinstance(c, comp_type))
