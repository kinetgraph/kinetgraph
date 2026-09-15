# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
testing.world_builder -- fluent SUT builders for WorldSystem tests.

The framework's systems are pure functions ``(World) -> list[Event]``
(ADR-018). Testing one is a matter of building the right ``World``
and calling the system against it. This module provides two fluent
builders that construct that state without mocks, without Redis, and
without fabricating ``Event`` envelopes:

  - :class:`AgentViewBuilder` -- builds a single ``AgentView`` with
    the components, trigger surface, and tool completions a system
    reads.
  - :class:`WorldBuilder` -- builds a ``World`` with one or more
    agents, keeping the ``ArchetypeStorage`` in sync with the views.

Why a builder (and not a ``make_world`` function)
--------------------------------------------------

The trigger surface is derived from the *existing* view fields
(``domain_phase`` + ``last_event_id`` + the component keyed by
``domain_phase``) -- see ADR-069 §11.16. A builder makes that
explicit and type-safe: ``with_trigger("invoice.approved")`` sets
``domain_phase`` and ``last_event_id`` together, so a test cannot
accidentally set one without the other. The fluent chain reads like
the state the system will observe, which is the point of a SUT
(System Under Test) builder.

No mocks, no monkey-patches. The builder only assembles state; the
system runs against the real ``World``. This follows the
``kntgraph-testing`` skill §7.4 rule: a test that needs a shim to
make a system observable is hiding a production bug.

Determinism. Systems that take an injected ``now`` (ADR-069 §12)
are called with a fixed clock in tests; the builder does not touch
the clock. Correlation is provided by wrapping the system call in
``correlation_middleware.scope()`` (ADR-037), which the builder's
``run`` helper does for you.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping
from uuid import UUID

from kntgraph.core.event.correlation import CorrelationContext, correlation_middleware
from kntgraph.core.world import World
from kntgraph.core.world.view import AgentView

if TYPE_CHECKING:
    from kntgraph.core._typing import JsonValue
    from kntgraph.core.world.components import (
        ToolCallCompletion,
        ToolCallRequest,
    )

__all__ = ["AgentViewBuilder", "WorldBuilder", "run_system"]


@dataclass
class AgentViewBuilder:
    """
    Fluent builder for a single ``AgentView``.

    The builder accumulates components, a trigger surface, and tool
    completions, then ``build()`` returns the immutable ``AgentView``.
    The builder itself is mutable (it is a test helper, not a
    framework value object).

    Example::

        view = (
            AgentViewBuilder("inv-1")
            .with_component(InvoiceDomainComponent(status="validating"))
            .with_component(ContinuityComponent(last_tools={"nfe_emitter": "..."}))
            .with_trigger("invoice.approved")
            .build()
        )
    """

    agent_id: str
    _components: dict[Any, Any] = field(default_factory=dict)
    _trigger_type: str | None = None
    _trigger_data: Mapping[str, "JsonValue"] = field(default_factory=dict)
    _tool_completions: Mapping[str, "ToolCallCompletion"] = field(default_factory=dict)
    _tool_requests: Mapping[str, "ToolCallRequest"] = field(default_factory=dict)
    _last_event_id: str | None = None

    def with_component(self, component: Any) -> "AgentViewBuilder":
        """Attach a component keyed by its class (the typed ECS
        convention, ADR-042). Returns ``self`` for chaining."""
        self._components[type(component)] = component
        return self

    def with_trigger(
        self,
        event_type: str,
        *,
        data: Mapping[str, "JsonValue"] | None = None,
    ) -> "AgentViewBuilder":
        """Declare the last domain event for the agent.

        Sets ``domain_phase`` (the trigger predicate) and
        ``last_event_id`` (a fresh deterministic id) together, and
        installs ``data`` as the component keyed by ``event_type``
        (mirroring the default fold, ADR-069 §11.16). Returns
        ``self`` for chaining.
        """
        self._trigger_type = event_type
        self._trigger_data = dict(data or {})
        self._components[event_type] = dict(data or {})
        return self

    def with_tool_completion(
        self,
        request_event_id: str,
        completion: "ToolCallCompletion",
    ) -> "AgentViewBuilder":
        """Attach a ``ToolCallCompletion`` to the ``tool_completions``
        slot, keyed by ``request_event_id`` (ADR-034). Returns
        ``self`` for chaining."""
        merged = dict(self._tool_completions)
        merged[request_event_id] = completion
        self._tool_completions = merged
        return self

    def with_tool_request(
        self,
        request: "ToolCallRequest",
    ) -> "AgentViewBuilder":
        """Attach a ``ToolCallRequest`` to the ``tool_requests``
        slot, keyed by ``request_event_id`` (ADR-034). The saga's
        ``_completion_for_step`` joins the request's ``tool_name``
        to the completion via this slot. Returns ``self`` for
        chaining."""
        merged = dict(self._tool_requests)
        merged[request.request_event_id] = request
        self._tool_requests = merged
        return self

    def with_last_event_id(self, event_id: str) -> "AgentViewBuilder":
        """Override the ``last_event_id`` explicitly (e.g. to seed a
        cursor-aware system's delta-scan). Returns ``self`` for
        chaining."""
        self._last_event_id = event_id
        return self

    def build(self) -> AgentView:
        """Return the immutable ``AgentView``.

        ``domain_phase`` is set to the trigger type (or ``None`` if
        no trigger was declared); ``last_event_id`` is the explicit
        override or a fresh deterministic id derived from the trigger
        type. ``tool_completions`` is installed as a derived slot.
        """
        components: dict[Any, Any] = dict(self._components)
        if self._tool_completions:
            components["tool_completions"] = self._tool_completions
        if self._tool_requests:
            components["tool_requests"] = self._tool_requests
        last_event_id = self._last_event_id
        if last_event_id is None and self._trigger_type is not None:
            last_event_id = _deterministic_id(self.agent_id, self._trigger_type)
        return AgentView(
            agent_id=self.agent_id,
            components=components,
            domain_phase=self._trigger_type,
            last_event_id=last_event_id,
        )


@dataclass
class WorldBuilder:
    """
    Fluent builder for a ``World`` with one or more agents.

    Keeps the ``ArchetypeStorage`` in sync with the views so
    ``world.query_agents(...)`` and ``world.get_agent(...)`` behave
    exactly as they do in production. Example::

        world = (
            WorldBuilder()
            .with_agent(AgentViewBuilder("inv-1").with_trigger("invoice.approved").build())
            .build()
        )
    """

    _views: dict[str, AgentView] = field(default_factory=dict)

    def with_agent(self, view: AgentView) -> "WorldBuilder":
        """Add an agent view. Returns ``self`` for chaining."""
        self._views[view.agent_id] = view
        return self

    def build(self) -> World:
        """Return the ``World`` with all added agents, its storage
        populated from the views' components.

        The storage is built directly from the views (via
        ``ArchetypeStorage.add_entity``) so ``query_agents`` and
        ``get_agent`` behave exactly as in production. No event is
        folded: the builder assembles the post-fold state, which is
        the SUT the system reads.
        """
        from kntgraph.core.storage import ArchetypeStorage

        storage = ArchetypeStorage()
        for view in self._views.values():
            if view.components:
                storage.add_entity(view.agent_id, dict(view.components))
        return World(tick=0, storage=storage, views=dict(self._views))


def _deterministic_id(agent_id: str, event_type: str) -> str:
    """A stable, readable id for a trigger, so a cursor-aware system
    sees a move and processes the trigger once. Not a real event id;
    it only needs to be unique per (agent, event_type). It is a
    valid UUID (uuid5) so systems that parse ``last_event_id`` as a
    UUID (e.g. ``FSMSystem`` building a ``causation_id``) do not
    fail."""
    from uuid import uuid5

    return str(
        uuid5(UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), f"{agent_id}|{event_type}")
    )


def run_system(
    system: Any,
    world: World,
    *,
    correlation: "CorrelationContext",
) -> list[Any]:
    """Invoke a ``WorldSystem`` against a ``World`` inside a
    correlation scope (ADR-037).

    **Mandatory invariant**: the caller MUST pass the entry
    event's ``CorrelationContext`` so the system under test
    inherits the flow id (mirrors the dispatcher's
    ``continue_from(...)`` path). Every test in this repo that
    exercises a ``WorldSystem`` is required to:

      1. Build a ``CorrelationContext`` for the flow id under
         test (a fresh ``uuid4`` for hermetic tests, a known
         UUID when the test asserts the audit trail end-to-end).
      2. Pass it via ``correlation=`` so the
         ``correlation_middleware`` carries it across the
         ``system(world)`` call.
      3. Assert every emitted event's ``correlation.correlation_id``
         equals the entry's via :func:`assert_correlation_id`
         (or the higher-level batch assertion
         :func:`assert_all_correlation_ids`).

    Rationale: ADR-037 §1.1 — the audit trail must stitch
    end-to-end from entry to the final completion. A test
    that lets ``run_system`` mint a fresh ``uuid4`` per call
    validates zero of the audit trail — a regression on the
    dispatcher side (e.g. dropping ``continue_from``) would
    not be caught. Mandatory ``correlation`` makes the invariant
    visible to the test suite.

    Per the ``WorldSystem`` Protocol, systems receive only
    the World (the post-fold projection). This helper
    mirrors the dispatcher's runtime behaviour: a single
    ``system(world)`` call inside a correlation scope.

    Example::

        flow_id = uuid4()
        ctx = CorrelationContext(correlation_id=flow_id)
        events = run_system(fsm, world, correlation=ctx)
        for e in events:
            assert_correlation_id(e, ctx)
    """
    if correlation is None:
        raise TypeError(
            "run_system requires an explicit `correlation` "
            "(CorrelationContext). The legacy fallback that "
            "minted a fresh uuid4() per call has been removed: "
            "tests that omit `correlation` no longer validate "
            "the audit trail (ADR-037 §1.1). Build the flow's "
            "CorrelationContext from the entry event (or a "
            "fresh uuid4() for hermetic tests) and pass it "
            "explicitly. See assert_correlation_id() for the "
            "matching assertion helper."
        )
    # Mirror the dispatcher's continue_from path: open a
    # ``correlation_middleware.scope(...)`` carrying the
    # entry's correlation_id so systems that pull
    # ``correlation_middleware.current()`` inherit the flow id.
    # The middleware's scope helper takes the correlation_id
    # explicitly; ``start`` would mutate the contextvar but
    # does NOT return a context manager.
    with correlation_middleware.scope(
        correlation_id=correlation.correlation_id,
    ):
        out = system(world)
        if not isinstance(out, list):
            import asyncio

            return asyncio.run(out)
        return out


def assert_correlation_id(
    event: Any,
    expected: "CorrelationContext | UUID",
) -> None:
    """Assert ``event.correlation.correlation_id`` equals
    ``expected.correlation_id`` (or the UUID directly).

    Every test that exercises a ``WorldSystem`` MUST call this
    on every emitted event so the audit trail invariant is
    enforced by the test suite (not just by reviewer
    vigilance). The framework's ``correlation_middleware`` is
    the single source of truth for the current flow id; this
    helper is the test-side mirror.
    """
    expected_id = (
        expected.correlation_id
        if isinstance(expected, CorrelationContext)
        else expected
    )
    actual_id = event.correlation.correlation_id
    assert actual_id == expected_id, (
        f"Audit trail broken: event {event.event_type} "
        f"(id={event.event_id}) has correlation_id {actual_id}, "
        f"expected {expected_id}; the WorldSystem failed to "
        f"inherit the entry event's flow id (ADR-037)."
    )


def assert_all_correlation_ids(
    events: list[Any],
    expected: "CorrelationContext | UUID",
) -> None:
    """Batch form of :func:`assert_correlation_id`: assert
    EVERY event in ``events`` carries ``expected``'s
    correlation_id. Convenience for the common pattern

        events = run_system(fsm, world, correlation=ctx)
        assert_all_correlation_ids(events, ctx)
    """
    expected_id = (
        expected.correlation_id
        if isinstance(expected, CorrelationContext)
        else expected
    )
    offenders = [
        e for e in events
        if e.correlation.correlation_id != expected_id
    ]
    assert not offenders, (
        f"Audit trail broken: {len(offenders)} of {len(events)} "
        f"events carry a different correlation_id than the "
        f"entry ({expected_id}); offenders: "
        f"{[(e.event_type, e.event_id, e.correlation.correlation_id) for e in offenders]}"
    )
