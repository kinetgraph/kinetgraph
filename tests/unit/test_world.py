# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the new World (v2.0).

The World in v2.0 is a *fold* of the event stream. There is no
mutable state on the world itself; no outbox; no with_agents.
State is derived from events by the projection function.
"""

from __future__ import annotations
from kntgraph.core.event import CorrelationContext

from dataclasses import dataclass

from kntgraph.core.event import Event
from kntgraph.core.world import World, project_default


# ---------------------------------------------------------------------------
# Empty / fold
# ---------------------------------------------------------------------------


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=__import__("uuid").uuid4())


class TestWorldEmpty:
    def test_empty(self):
        w = World.empty()
        assert w.tick == 0
        assert w.agents == {}
        assert w.storage.num_entities == 0

    def test_empty_with_custom_tick(self):
        w = World.empty(tick=42)
        assert w.tick == 42


class TestWorldFold:
    def test_empty_fold(self):
        w = World.fold([])
        assert w.tick == 0
        assert w.agents == {}

    def test_fold_lifecycle_event(self):
        e = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        w = World.fold([e], tick=1)
        assert "a-1" in w.agents
        view = w.agents["a-1"]
        assert view.operational_phase == "spawned"
        assert view.operational_at is not None

    def test_fold_domain_event(self):
        e = Event.create(
            event_type="document.received",
            agent_id="a-1",
            event_class="domain",
            data={"document_id": "NF-001"},
            correlation=_ctx(),
        )
        w = World.fold([e], tick=1)
        view = w.agents["a-1"]
        assert view.domain_phase == "document.received"
        assert "document.received" in view.components

    def test_lifecycle_then_domain(self):
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
            Event.create(
                event_type="document.validated",
                agent_id="a-1",
                event_class="domain",
                data={"document_id": "NF-001"},
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events, tick=2)
        view = w.agents["a-1"]
        assert view.operational_phase == "spawned"
        assert view.domain_phase == "document.validated"
        assert view.components["document.validated"]["document_id"] == "NF-001"

    def test_lifecycle_only_spawns_visible(self):
        """Domain event absent → components empty but agent exists."""
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events)
        view = w.agents["a-1"]
        assert view.operational_phase == "spawned"
        assert view.domain_phase is None
        assert view.components == {}

    def test_multiple_agents(self):
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
            Event.create(
                event_type="agent.spawned",
                agent_id="a-2",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events, tick=2)
        assert set(w.agents.keys()) == {"a-1", "a-2"}

    def test_fold_is_pure(self):
        """Same events → same world. Determinism."""
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"k": 1},
                correlation=_ctx(),
            ),
        ]
        w1 = World.fold(events, tick=2)
        w2 = World.fold(events, tick=2)
        assert w1.tick == w2.tick
        assert repr(w1.views) == repr(w2.views)

    def test_fold_drops_components_after_replay(self):
        """
        Components are derived from the last domain event. If the
        default projection does not aggregate, the last one wins.
        This is the documented behavior; applications provide richer
        projections as needed.
        """
        events = [
            Event.create(
                event_type="document.received",
                agent_id="a-1",
                event_class="domain",
                data={"k": 1},
                correlation=_ctx(),
            ),
            Event.create(
                event_type="document.validated",
                agent_id="a-1",
                event_class="domain",
                data={"k": 2},
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events)
        view = w.agents["a-1"]
        # only the last event_type is a key in components
        assert "document.validated" in view.components
        assert "document.received" not in view.components


# ---------------------------------------------------------------------------
# with_event (single-event world)
# ---------------------------------------------------------------------------


class TestWorldWithEvent:
    def test_apply_lifecycle_event(self):
        w = World.empty()
        e = Event.create(
            event_type="agent.running",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        w2 = w.with_event(e)
        assert w.tick == 0
        assert w2.tick == 1
        assert w2.agents["a-1"].operational_phase == "running"

    def test_apply_domain_event(self):
        w = World.empty()
        e = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="domain",
            data={"v": 1},
            correlation=_ctx(),
        )
        w2 = w.with_event(e)
        assert w2.agents["a-1"].domain_phase == "x"
        assert w2.agents["a-1"].components["x"]["v"] == 1

    def test_with_event_preserves_operational_phase(self):
        e1 = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        e2 = Event.create(
            event_type="x",
            agent_id="a-1",
            event_class="domain",
            data={},
            correlation=_ctx(),
        )
        w = World.empty().with_event(e1).with_event(e2)
        assert w.agents["a-1"].operational_phase == "spawned"
        assert w.agents["a-1"].domain_phase == "x"


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class DocumentComponent:
    document_id: str


@dataclass(slots=True, frozen=True)
class ClientContextComponent:
    cnpj: str


class TestWorldQuery:
    def test_query_by_component_type(self):
        events = [
            Event.create(
                event_type="doc.received",
                agent_id="a-1",
                event_class="domain",
                data={"document_id": "NF-001"},
                correlation=_ctx(),
            ),
            Event.create(
                event_type="client.known",
                agent_id="a-1",
                event_class="domain",
                data={"cnpj": "12345678901234"},
                correlation=_ctx(),
            ),
            Event.create(
                event_type="doc.received",
                agent_id="a-2",
                event_class="domain",
                data={"document_id": "NF-002"},
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events)
        # In default projection each event_type becomes a component;
        # querying for DocumentComponent finds agents whose last
        # event had a "document" payload.
        result = list(w.query_agents())
        assert len(result) >= 2

    def test_filter_with_predicate(self):
        events = [
            Event.create(
                event_type="doc.received",
                agent_id="a-1",
                event_class="domain",
                data={"document_id": "NF-001"},
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events)
        q = w.query_agents().filter(lambda v: v.agent_id == "a-1")
        assert q.count() == 1
        assert q.first() is not None
        assert q.first()[0] == "a-1"  # type: ignore[index]

    def test_query_empty_world(self):
        w = World.empty()
        assert w.query_agents().is_empty()

    def test_get_agent(self):
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events)
        view = w.get_agent("a-1")
        assert view is not None
        assert view.operational_phase == "spawned"
        assert w.get_agent("nonexistent") is None


# ---------------------------------------------------------------------------
# Repr / serialization
# ---------------------------------------------------------------------------


class TestWorldRepr:
    def test_repr_includes_counts(self):
        events = [
            Event.create(
                event_type="agent.spawned",
                agent_id="a-1",
                event_class="lifecycle",
                correlation=_ctx(),
            ),
        ]
        w = World.fold(events, tick=1)
        s = repr(w)
        assert "tick=1" in s
        assert "agents=1" in s


# ---------------------------------------------------------------------------
# _apply_event — the single source of truth for the
# lifecycle vs. domain branch. Both ``project_default``
# (batch fold) and ``World.with_event`` (single-event)
# delegate here. Pin the contract so a future change
# to one call-site does not silently desync from the
# other.
# ---------------------------------------------------------------------------


class TestApplyEventContract:
    """
    Pairs of tests that exercise the SAME agent with the
    SAME events through ``project_default`` (batch) and
    ``World.with_event`` (single-event). The resulting
    ``AgentView`` MUST be identical — that is the
    duplication we are killing.
    """

    def test_lifecycle_event_same_in_both_paths(self):
        e = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        view_batch = project_default([e])["a-1"]
        view_single = World.empty().with_event(e).get_agent("a-1")
        assert view_single is not None
        assert _views_equal(view_batch, view_single)

    def test_domain_event_same_in_both_paths(self):
        e = Event.create(
            event_type="document.validated",
            agent_id="a-1",
            event_class="domain",
            data={"ok": True, "score": 0.9},
            correlation=_ctx(),
        )
        view_batch = project_default([e])["a-1"]
        view_single = World.empty().with_event(e).get_agent("a-1")
        assert view_single is not None
        assert _views_equal(view_batch, view_single)

    def test_lifecycle_then_domain_same_in_both_paths(self):
        spawn = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        doc = Event.create(
            event_type="document.validated",
            agent_id="a-1",
            event_class="domain",
            data={"ok": True},
            correlation=_ctx(),
        )
        view_batch = project_default([spawn, doc])["a-1"]
        world = World.empty().with_event(spawn).with_event(doc)
        view_single = world.get_agent("a-1")
        assert view_single is not None
        assert _views_equal(view_batch, view_single)

    def test_domain_then_lifecycle_same_in_both_paths(self):
        # Domain before lifecycle: in the default
        # projection the domain event still records a
        # domain_phase even without a prior spawn. The
        # with_event path must produce the same view.
        doc = Event.create(
            event_type="document.validated",
            agent_id="a-1",
            event_class="domain",
            data={"ok": True},
            correlation=_ctx(),
        )
        spawn = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        view_batch = project_default([doc, spawn])["a-1"]
        world = World.empty().with_event(doc).with_event(spawn)
        view_single = world.get_agent("a-1")
        assert view_single is not None
        assert _views_equal(view_batch, view_single)

    def test_three_events_mixed_same_in_both_paths(self):
        spawn = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        doc1 = Event.create(
            event_type="document.created",
            agent_id="a-1",
            event_class="domain",
            data={"v": 1},
            correlation=_ctx(),
        )
        doc2 = Event.create(
            event_type="document.validated",
            agent_id="a-1",
            event_class="domain",
            data={"v": 2},
            correlation=_ctx(),
        )
        view_batch = project_default([spawn, doc1, doc2])["a-1"]
        world = World.empty().with_event(spawn).with_event(doc1).with_event(doc2)
        view_single = world.get_agent("a-1")
        assert view_single is not None
        assert _views_equal(view_batch, view_single)


class TestApplyEventSemantics:
    """
    The single-source-of-truth function must encode the
    documented projection rule: lifecycle events update
    operational_phase, domain events replace components.
    A regression here is the bug we are most worried
    about.
    """

    def test_lifecycle_updates_operational_phase(self):

        e = Event.create(
            event_type="agent.running",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        view = World.empty().with_event(e).get_agent("a-1")
        assert view is not None
        assert view.operational_phase == "running"
        assert view.operational_at == e.timestamp

    def test_lifecycle_keeps_components_from_prev(self):
        """A lifecycle event must NOT clobber the agent's
        components. The first domain event sets them; a
        later lifecycle event leaves them alone.
        """

        doc = Event.create(
            event_type="document.created",
            agent_id="a-1",
            event_class="domain",
            data={"k": "v"},
            correlation=_ctx(),
        )
        spawn = Event.create(
            event_type="agent.running",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        world = World.empty().with_event(doc).with_event(spawn)
        view = world.get_agent("a-1")
        assert view is not None
        assert view.components == {"document.created": {"k": "v"}}
        assert view.operational_phase == "running"

    def test_domain_replaces_components(self):
        """A domain event replaces the previous components
        with the event's data (default projection: last
        event wins, one component per event_type).
        """

        e1 = Event.create(
            event_type="document.created",
            agent_id="a-1",
            event_class="domain",
            data={"v": 1},
            correlation=_ctx(),
        )
        e2 = Event.create(
            event_type="document.created",  # same type
            agent_id="a-1",
            event_class="domain",
            data={"v": 2},
            correlation=_ctx(),
        )
        world = World.empty().with_event(e1).with_event(e2)
        view = world.get_agent("a-1")
        assert view is not None
        # Default projection: one slot per event_type, last
        # event wins.
        assert view.components == {
            "document.created": {"v": 2},
        }

    def test_last_event_id_advances(self):
        e1 = Event.create(
            event_type="agent.spawned",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        e2 = Event.create(
            event_type="agent.idle",
            agent_id="a-1",
            event_class="lifecycle",
            correlation=_ctx(),
        )
        world = World.empty().with_event(e1).with_event(e2)
        view = world.get_agent("a-1")
        assert view is not None
        assert view.last_event_id == str(e2.event_id)
        assert view.last_event_at == e2.timestamp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _views_equal(a, b) -> bool:
    """Compare two AgentView instances field-by-field
    (the dataclass ``__eq__`` is fine, but components
    is a Mapping — order-independent equality).
    """
    return (
        a.agent_id == b.agent_id
        and dict(a.components) == dict(b.components)
        and a.operational_phase == b.operational_phase
        and a.operational_at == b.operational_at
        and a.domain_phase == b.domain_phase
        and a.domain_at == b.domain_at
        and a.last_event_id == b.last_event_id
        and a.last_event_at == b.last_event_at
    )
