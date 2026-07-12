# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from uuid import uuid4
import pytest

from kntgraph.agents.roles import IntentComponent, IntentResolutionSystem, RoleComponent
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.world import World
from kntgraph.core.world.view import AgentView
from kntgraph.core.storage import ArchetypeStorage
from kntgraph.tools.registry import ToolRegistry
from kntgraph.tools.acl import ToolACL
from kntgraph.security import Principal, Role


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "fake tool"
        self.input_schema = {}


def _correlation() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid4())


class TestIntentResolutionSystem:
    def test_ignores_non_pending_intents(self) -> None:
        registry = ToolRegistry()
        system = IntentResolutionSystem(registry)

        role = RoleComponent(persona="test", instructions="test", allowed_tools=["calculator"])
        intent = IntentComponent(
            target_tool="calculator",
            parameters={"a": 1, "b": 2},
            status="completed",  # Not pending!
            correlation=_correlation(),
        )
        principal = Principal(agent_id="agent-1", role=Role.agent, tenant_id="tenant-1", key_id="key-1")

        views = {
            "agent-1": AgentView(
                agent_id="agent-1",
                components={"role": role, "intent": intent, "principal": principal},
                operational_phase="running",
                operational_at=None,
                domain_phase=None,
                domain_at=None,
                last_event_id="evt-1",
                last_event_at=None,
            )
        }
        storage = ArchetypeStorage()
        storage.add_entity("agent-1", {"role": role, "intent": intent, "principal": principal})
        world = World(tick=1, storage=storage, views=views)

        events = system(world)
        assert len(events) == 0

    def test_fails_when_tool_not_found(self) -> None:
        registry = ToolRegistry()
        system = IntentResolutionSystem(registry)

        role = RoleComponent(persona="test", instructions="test", allowed_tools=["calculator"])
        corr = _correlation()
        intent = IntentComponent(
            target_tool="calculator",
            parameters={"a": 1, "b": 2},
            status="pending",
            correlation=corr,
        )
        principal = Principal(agent_id="agent-1", role=Role.agent, tenant_id="tenant-1", key_id="key-1")

        views = {
            "agent-1": AgentView(
                agent_id="agent-1",
                components={"role": role, "intent": intent, "principal": principal},
                operational_phase="running",
                operational_at=None,
                domain_phase=None,
                domain_at=None,
                last_event_id="evt-1",
                last_event_at=None,
            )
        }
        storage = ArchetypeStorage()
        storage.add_entity("agent-1", {"role": role, "intent": intent, "principal": principal})
        world = World(tick=1, storage=storage, views=views)

        events = system(world)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "intent.validation_failed"
        assert ev.data["reason"] == "Tool calculator not found."
        assert ev.correlation.correlation_id == corr.correlation_id

    def test_fails_when_acl_denied(self) -> None:
        registry = ToolRegistry()
        tool = FakeTool("calculator")
        # Tool only allowed for admins
        acl = ToolACL(required_role=Role.admin)
        registry.register(tool, acl=acl)

        system = IntentResolutionSystem(registry)

        role = RoleComponent(persona="test", instructions="test", allowed_tools=["calculator"])
        corr = _correlation()
        intent = IntentComponent(
            target_tool="calculator",
            parameters={"a": 1, "b": 2},
            status="pending",
            correlation=corr,
        )
        # Principal is only agent
        principal = Principal(agent_id="agent-1", role=Role.agent, tenant_id="tenant-1", key_id="key-1")

        views = {
            "agent-1": AgentView(
                agent_id="agent-1",
                components={"role": role, "intent": intent, "principal": principal},
                operational_phase="running",
                operational_at=None,
                domain_phase=None,
                domain_at=None,
                last_event_id="evt-1",
                last_event_at=None,
            )
        }
        storage = ArchetypeStorage()
        storage.add_entity("agent-1", {"role": role, "intent": intent, "principal": principal})
        world = World(tick=1, storage=storage, views=views)

        events = system(world)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "intent.validation_failed"
        assert "ACL Denied" in ev.data["reason"]
        assert ev.correlation.correlation_id == corr.correlation_id

    def test_fails_when_semantic_role_unauthorized(self) -> None:
        registry = ToolRegistry()
        tool = FakeTool("calculator")
        registry.register(tool)

        system = IntentResolutionSystem(registry)

        # tool is not in allowed_tools list for this role
        role = RoleComponent(persona="test", instructions="test", allowed_tools=[])
        corr = _correlation()
        intent = IntentComponent(
            target_tool="calculator",
            parameters={"a": 1, "b": 2},
            status="pending",
            correlation=corr,
        )
        principal = Principal(agent_id="agent-1", role=Role.agent, tenant_id="tenant-1", key_id="key-1")

        views = {
            "agent-1": AgentView(
                agent_id="agent-1",
                components={"role": role, "intent": intent, "principal": principal},
                operational_phase="running",
                operational_at=None,
                domain_phase=None,
                domain_at=None,
                last_event_id="evt-1",
                last_event_at=None,
            )
        }
        storage = ArchetypeStorage()
        storage.add_entity("agent-1", {"role": role, "intent": intent, "principal": principal})
        world = World(tick=1, storage=storage, views=views)

        events = system(world)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "intent.validation_failed"
        assert ev.data["reason"] == "Semantic Role not authorized."
        assert ev.correlation.correlation_id == corr.correlation_id

    def test_emits_tool_requested_on_success(self) -> None:
        registry = ToolRegistry()
        tool = FakeTool("calculator")
        registry.register(tool)

        system = IntentResolutionSystem(registry)

        role = RoleComponent(persona="test", instructions="test", allowed_tools=["calculator"])
        corr = _correlation()
        intent = IntentComponent(
            target_tool="calculator",
            parameters={"a": 1, "b": 2},
            status="pending",
            correlation=corr,
        )
        principal = Principal(agent_id="agent-1", role=Role.agent, tenant_id="tenant-1", key_id="key-1")

        views = {
            "agent-1": AgentView(
                agent_id="agent-1",
                components={"role": role, "intent": intent, "principal": principal},
                operational_phase="running",
                operational_at=None,
                domain_phase=None,
                domain_at=None,
                last_event_id="evt-1",
                last_event_at=None,
            )
        }
        storage = ArchetypeStorage()
        storage.add_entity("agent-1", {"role": role, "intent": intent, "principal": principal})
        world = World(tick=1, storage=storage, views=views)

        events = system(world)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "tool.calculator.requested"
        assert ev.data["tool"] == "calculator"
        assert ev.data["params"] == {"a": 1, "b": 2}
        assert ev.causation_id == "evt-1"
        assert ev.correlation.correlation_id == corr.correlation_id
