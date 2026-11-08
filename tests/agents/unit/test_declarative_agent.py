# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the declarative agent model (ADR-088).

Covers ``RoleSpec``, ``AgentDefinition``, ``AgentBundle`` and
``RoleProjection``. Every public function is exercised on its happy
path and on at least one failure mode.

Behaviour style: real components, real views, a real world. The
dispatcher is the real one -- ``install`` only calls its public
registration API.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from kntgraph.agents import AgentBundle, AgentDefinition, RoleProjection, RoleSpec
from kntgraph.concordos import Concordo
from kntgraph.concordos.fsm import (
    BusinessFSMConcordo,
    FSMConfig,
    FSMTransition,
)
from kntgraph.concordos.saga import SagaConfig, SagaStepConfig, WorkflowSagaConcordo
from kntgraph.core.components.role import RoleComponent, has_tool_access
from kntgraph.core.event import Event
from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.world import DomainComponent, World
from kntgraph.infra.world_checkpoint import WorldCheckpoint
from kntgraph.runner.reactive import ReactiveDispatcher
from kntgraph.security.principal import Principal, PrincipalLevel
from kntgraph.testing import AgentViewBuilder, WorldBuilder

ROLE_EVENT = "role.assigned"


@dataclass(frozen=True, slots=True)
class OrderComponent(DomainComponent):
    """A domain component for the fixtures."""

    status: str = "draft"


def _fsm_config() -> FSMConfig:
    return FSMConfig(
        component_type=OrderComponent,
        state_field="status",
        transitions={
            "draft": {"order.submitted": FSMTransition(to="validating")},
            "validating": {"order.approved": FSMTransition(to="approved")},
        },
        terminal=frozenset({"cancelled"}),
    )


def _saga_config() -> SagaConfig:
    return SagaConfig(
        name="OrderFulfilment",
        steps=(
            SagaStepConfig(
                name="reserve",
                tool_name="reserve_inventory",
                compensate_tool="release_inventory",
            ),
        ),
    )


def _definition(**overrides: object) -> AgentDefinition:
    """A minimal declaration, overridable per test."""
    fields: dict[str, object] = {
        "name": "checkout",
        "persona": RoleSpec(
            persona="fulfilment",
            allowed_tools=("reserve_inventory", "charge_payment"),
        ),
        "role_event_type": ROLE_EVENT,
    }
    fields.update(overrides)
    return AgentDefinition(**fields)  # type: ignore[arg-type]


def _role_event(
    agent_id: str = "agent-1",
    *,
    persona: str | None = "fulfilment",
    instructions: str | None = "You fulfil orders.",
) -> Event:
    """A role event with the given payload keys."""
    data: dict[str, str] = {}
    if persona is not None:
        data["persona"] = persona
    if instructions is not None:
        data["instructions"] = instructions
    return Event.create(
        event_type=ROLE_EVENT,
        agent_id=agent_id,
        event_class="domain",
        correlation=CorrelationContext.new(),
        data=data,
    )


def _world_with_agent(agent_id: str = "agent-1"):
    """A world holding one agent view."""
    return (
        WorldBuilder()
        .with_agent(AgentViewBuilder(agent_id).with_trigger("order.submitted").build())
        .build()
    )


class _StubEventLog:
    """The minimal event-log surface the dispatcher's constructor uses.

    ``install`` only calls the dispatcher's registration API, so the
    log is never exercised. The stub exists because the dispatcher
    requires one -- the same pattern the runner's unit tests use.
    """

    async def read_after_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        return [], cursor

    async def list_agents(self) -> list[str]:
        return []

    async def append_batch(self, events: list[Event]) -> list[str]:
        return ["ok"] * len(events)


class _StubWorldStore:
    """The minimal world-store surface the dispatcher's constructor uses.

    Like the log, it is never exercised: ``install`` only calls the
    registration API.
    """

    async def load(self, agent_id: str) -> WorldCheckpoint:
        return WorldCheckpoint(world=World.empty(), last_stream_id="-")

    async def save(self, agent_id: str, checkpoint: WorldCheckpoint) -> None:
        return None


def _dispatcher() -> ReactiveDispatcher:
    """A dispatcher with no systems, ready to receive an agent."""
    return ReactiveDispatcher(
        log=_StubEventLog(),
        world_store=_StubWorldStore(),
        systems=[],
    )


def _principal(agent_id: str = "agent-1") -> Principal:
    return Principal(
        agent_id=agent_id,
        level=PrincipalLevel.agent,
        tenant_id="tenant-a",
        key_id="key-1",
    )


# ---------------------------------------------------------------------------
# RoleSpec
# ---------------------------------------------------------------------------


def test_role_spec_is_frozen_and_carries_no_runtime_handle() -> None:
    """The spec is frozen, slotted, and holds no runtime handle."""
    spec = RoleSpec(persona="fulfilment", allowed_tools=("reserve_inventory",))

    assert spec.persona == "fulfilment"
    assert spec.allowed_tools == ("reserve_inventory",)
    # Frozen: the model cannot be mutated after construction.
    with pytest.raises(FrozenInstanceError):
        spec.persona = "other"  # type: ignore[misc]
    # No runtime handle: the spec's fields are configuration only.
    assert set(spec.__slots__) == {"persona", "allowed_tools"}


def test_role_spec_to_component_projects_the_policy() -> None:
    """The conversion produces the component gate 2 reads."""
    spec = RoleSpec(persona="fulfilment", allowed_tools=("reserve_inventory",))

    component = spec.to_component(instructions="You fulfil orders.")

    assert component.persona == "fulfilment"
    assert component.instructions == "You fulfil orders."
    assert component.allowed_tools == ["reserve_inventory"]


def test_role_spec_to_component_prefers_the_runtime_persona() -> None:
    """A runtime-assigned persona overrides the declared one."""
    spec = RoleSpec(persona="declared")

    component = spec.to_component(instructions="x", persona="assigned")

    assert component.persona == "assigned"


# ---------------------------------------------------------------------------
# AgentBundle
# ---------------------------------------------------------------------------


def test_agent_bundle_satisfies_the_concordo_protocol() -> None:
    """The bundle is installable through the catalog path (ADR-069 §3.2)."""
    bundle = _definition().bundle()

    assert isinstance(bundle, Concordo)
    assert bundle.name == "agent:checkout"


def test_bundle_derives_systems_and_projections() -> None:
    """The bundle carries the FSM's and the sagas' runtime objects."""
    definition = _definition(
        fsm=BusinessFSMConcordo(config=_fsm_config()),
        sagas=(WorkflowSagaConcordo(config=_saga_config()),),
    )

    bundle = definition.bundle()

    system_names = [type(system).__name__ for system in bundle.systems]
    assert system_names == ["FSMSystem", "SagaSystem", "SagaTimeoutSystem"]
    projection_names = [type(p).__name__ for p in bundle.projections]
    assert projection_names == ["FSMProjection", "SagaProjection", "RoleProjection"]
    # The bundle repeats no field of the definition.
    assert not hasattr(bundle, "persona")
    assert not hasattr(bundle, "fsm")
    assert not hasattr(bundle, "sagas")


def test_bundle_without_fsm_derives_only_the_role_projection() -> None:
    """A declaration with no FSM still derives its role projection."""
    bundle = _definition().bundle()

    assert bundle.systems == ()
    assert [type(p).__name__ for p in bundle.projections] == ["RoleProjection"]


def test_bundle_is_ephemeral_and_does_not_mutate_the_definition() -> None:
    """Deriving twice yields equal bundles; the definition is unchanged."""
    definition = _definition(fsm=BusinessFSMConcordo(config=_fsm_config()))

    first = definition.bundle()
    second = definition.bundle()

    assert first.name == second.name
    assert len(first.systems) == len(second.systems)
    assert definition.fsm is not None


# ---------------------------------------------------------------------------
# AgentDefinition.install
# ---------------------------------------------------------------------------


def test_install_registers_systems_and_projections() -> None:
    """Installation registers the derived runtime on the dispatcher."""
    definition = _definition(
        fsm=BusinessFSMConcordo(config=_fsm_config()),
        sagas=(WorkflowSagaConcordo(config=_saga_config()),),
    )
    dispatcher = _dispatcher()

    definition.install(dispatcher, principal=_principal())

    bundle = definition.bundle()
    assert len(dispatcher.systems) == len(bundle.systems)
    # The role projection is registered after the concordo projections.
    assert isinstance(dispatcher._projections[-1], RoleProjection)


def test_install_tracks_the_agent_named_by_the_principal() -> None:
    """The principal identifies the agent the dispatcher must watch."""
    dispatcher = _dispatcher()

    _definition().install(dispatcher, principal=_principal("agent-42"))

    assert "agent-42" in dispatcher._tracked_agents


def test_install_is_idempotent_in_its_effect_on_the_world() -> None:
    """Installing twice registers twice -- the host owns the lifecycle.

    ``install`` registers; the host decides whether to install. This
    test pins the behaviour so it cannot change silently.
    """
    dispatcher = _dispatcher()
    definition = _definition()

    definition.install(dispatcher, principal=_principal())
    definition.install(dispatcher, principal=_principal())

    assert len(dispatcher.systems) == 0
    assert len(dispatcher._projections) == 2


# ---------------------------------------------------------------------------
# RoleProjection
# ---------------------------------------------------------------------------


def test_role_projection_materialises_role_component() -> None:
    """The role event materialises the component on the agent's view."""
    projection = RoleProjection(
        spec=RoleSpec(persona="declared", allowed_tools=("reserve_inventory",)),
        event_type=ROLE_EVENT,
    )
    world = _world_with_agent()

    projected = projection(world, [_role_event()])

    view = projected.get_agent("agent-1")
    assert view is not None
    component = view.get_component(RoleComponent)
    assert component is not None
    assert component.persona == "fulfilment"
    assert component.instructions == "You fulfil orders."
    assert component.allowed_tools == ["reserve_inventory"]


def test_role_projection_ignores_other_event_types() -> None:
    """A batch without a role event leaves the world untouched."""
    projection = RoleProjection(
        spec=RoleSpec(persona="declared", allowed_tools=("reserve_inventory",)),
        event_type=ROLE_EVENT,
    )
    world = _world_with_agent()
    other = Event.create(
        event_type="order.submitted",
        agent_id="agent-1",
        event_class="domain",
        correlation=CorrelationContext.new(),
    )

    projected = projection(world, [other])

    assert projected is world
    view = projected.get_agent("agent-1")
    assert view is not None
    assert view.get_component(RoleComponent) is None


def test_role_projection_keeps_the_policy_when_a_payload_key_is_missing() -> None:
    """A malformed payload still yields the declared capability policy.

    Dropping the component would silently disable gate 2, because
    ``has_tool_access`` is permissive when no role is installed. The
    policy is static configuration; it must not depend on the
    well-formedness of the event.
    """
    projection = RoleProjection(
        spec=RoleSpec(persona="declared", allowed_tools=("reserve_inventory",)),
        event_type=ROLE_EVENT,
    )
    world = _world_with_agent()

    projected = projection(world, [_role_event(persona=None, instructions=None)])

    view = projected.get_agent("agent-1")
    assert view is not None
    component = view.get_component(RoleComponent)
    assert component is not None
    # The declared persona is the fallback.
    assert component.persona == "declared"
    assert component.instructions == ""
    assert component.allowed_tools == ["reserve_inventory"]


def test_role_projection_skips_agents_without_a_view() -> None:
    """An event for an unknown agent is ignored, not fatal."""
    projection = RoleProjection(
        spec=RoleSpec(persona="declared"),
        event_type=ROLE_EVENT,
    )
    world = _world_with_agent("agent-1")

    projected = projection(world, [_role_event("agent-unknown")])

    assert projected is world


# ---------------------------------------------------------------------------
# Gate 2
# ---------------------------------------------------------------------------


def test_projected_component_is_what_gate_two_reads() -> None:
    """Gate 2 reads the projected component, not the spec directly."""
    spec = RoleSpec(persona="fulfilment", allowed_tools=("reserve_inventory",))
    projection = RoleProjection(spec=spec, event_type=ROLE_EVENT)

    projected = projection(_world_with_agent(), [_role_event()])

    view = projected.get_agent("agent-1")
    assert view is not None
    role = view.get_component(RoleComponent)
    assert role is not None
    assert has_tool_access(role, "reserve_inventory") is True
    assert has_tool_access(role, "charge_payment") is False


def test_agent_bundle_exposes_no_credential() -> None:
    """The bundle holds no runtime credential."""
    bundle = _definition().bundle()

    assert not hasattr(bundle, "principal")
    assert not hasattr(bundle, "key_id")


def test_agent_definition_is_frozen() -> None:
    """The declaration cannot be mutated after construction."""
    definition = _definition()

    with pytest.raises(FrozenInstanceError):
        definition.name = "other"  # type: ignore[misc]


def test_agent_bundle_is_frozen() -> None:
    """The derived bundle cannot be mutated after construction."""
    bundle = _definition().bundle()

    with pytest.raises(FrozenInstanceError):
        bundle.name = "other"  # type: ignore[misc]


def test_agent_bundle_type_is_exported() -> None:
    """The bundle type is reachable from the package's public API."""
    assert AgentBundle.__name__ == "AgentBundle"
