# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``runner.reactive_tool_projection``
helpers.

The full projection behavior (end-to-end with the
dispatcher) is covered by
``test_reactive_dispatcher_projection.py``. These
tests focus on the two extracted helpers in
isolation:

  - ``_has_tool_events``: pre-check for any tool
    event in the batch.
  - ``_overlay_tool_projection``: applies the
    projection to a World, mutating ``storage`` and
    ``views`` only for affected agents.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Optional
from uuid import uuid4

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.storage import ArchetypeStorage
from kntgraph.core.world import World
from kntgraph.runner.reactive_tool_projection import (
    _has_tool_events,
    _overlay_tool_projection,
)


def _ctx() -> CorrelationContext:
    return CorrelationContext.new(correlation_id=uuid4())


def _event(
    *,
    event_type: str,
    agent_id: str = "a-1",
    data: Optional[dict] = None,
    causation_id: Optional[Any] = None,
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=MappingProxyType(dict(data or {})),
        correlation=_ctx(),
        causation_id=causation_id,
    )


def _empty_world() -> World:
    return World(tick=0, storage=ArchetypeStorage(), views={})


# ---------------------------------------------------------------------------
# _has_tool_events
# ---------------------------------------------------------------------------


class TestHasToolEvents:
    def test_empty_batch_returns_false(self):
        assert _has_tool_events([]) is False

    def test_non_tool_events_return_false(self):
        events = [
            _event(event_type="user.intent"),
            _event(event_type="domain.tick"),
        ]
        assert _has_tool_events(events) is False

    def test_tool_requested_event_detected(self):
        """The bare ``tool.requested`` event is
        detected (no suffix)."""
        events = [_event(event_type="tool.requested")]
        assert _has_tool_events(events) is True

    def test_tool_completed_event_detected(self):
        events = [_event(event_type="tool.pii.completed")]
        assert _has_tool_events(events) is True

    def test_tool_failed_event_detected(self):
        events = [_event(event_type="tool.pii.failed")]
        assert _has_tool_events(events) is True

    def test_tool_event_with_dotted_name_detected(self):
        """Tool names can contain dots (e.g.
        ``invoice.issue``). The helper matches by
        prefix ``tool.`` and suffix ``.completed`` /
        ``.failed``."""
        events = [_event(event_type="tool.invoice.issue.completed")]
        assert _has_tool_events(events) is True

    def test_event_starting_with_tool_but_other_suffix_ignored(self):
        """``tool.x.args_invalid`` is not a tool
        completion — the helper only matches
        ``.completed`` and ``.failed`` suffixes."""
        events = [_event(event_type="tool.x.args_invalid")]
        assert _has_tool_events(events) is False

    def test_named_tool_requested_event_detected(self):
        """Regression: a canonical ``tool.<name>.requested``
        event (the form emitted by
        ``ToolAwareSystem.request_tool``) is detected.
        Before the fix, only the legacy bare
        ``tool.requested`` form was recognised, so the
        dispatcher skipped the projection pass and
        ``ToolCallRequest`` was never installed."""
        events = [_event(event_type="tool.weather_api.requested")]
        assert _has_tool_events(events) is True

    def test_mixed_batch_with_one_tool_event_detected(self):
        events = [
            _event(event_type="user.intent"),
            _event(event_type="tool.requested"),
            _event(event_type="domain.tick"),
        ]
        assert _has_tool_events(events) is True


# ---------------------------------------------------------------------------
# _overlay_tool_projection
# ---------------------------------------------------------------------------


class TestOverlayToolProjection:
    def test_empty_events_returns_same_world(self):
        """No events → no projection work → the same
        World object (no allocation)."""
        world = _empty_world()
        result = _overlay_tool_projection(world, [])
        assert result is world

    def test_non_tool_events_returns_same_world(self):
        """Non-tool events don't trigger the
        projection — the same World is returned."""
        world = _empty_world()
        events = [_event(event_type="user.intent")]
        result = _overlay_tool_projection(world, events)
        assert result is world

    def test_tool_requested_installs_slot(self):
        """A ``tool.requested`` event installs the
        ``tool_requests`` slot on the affected
        agent's view.

        The helper operates on a post-fold World:
        the events must already be folded into the
        World via ``World.with_event`` so the
        underlying storage has the request metadata.
        """
        world = _empty_world()
        req = _event(event_type="tool.requested", agent_id="a-1")
        # Fold the request into the world first (this
        # is what `_fold_with_filter` does before
        # calling the overlay).
        world = world.with_event(req)
        result = _overlay_tool_projection(world, [req])
        assert result is not world
        # The view for "a-1" should now carry a
        # tool_requests component with the request id.
        assert "a-1" in result.views
        view = result.views["a-1"]
        assert "tool_requests" in view.components
        assert str(req.event_id) in view.components["tool_requests"]

    def test_untouched_agent_views_preserved(self):
        """When only one agent has tool events, the
        other agents' views are passed through
        (same object, no allocation)."""
        world = _empty_world()
        events = [_event(event_type="tool.requested", agent_id="a-1")]
        result = _overlay_tool_projection(world, events)
        # Only "a-1" was added to views; "a-2" never
        # appeared, so the view dict should not
        # contain it.
        assert "a-2" not in result.views

    def test_completion_event_installs_completion_slot(self):
        """A ``tool.<name>.completed`` event installs
        the ``tool_completions`` slot."""
        world = _empty_world()
        req = _event(event_type="tool.requested", agent_id="a-1")
        completion = _event(
            event_type="tool.lookup.completed",
            agent_id="a-1",
            causation_id=req.event_id,
        )
        # Fold both events first.
        world = world.with_event(req).with_event(completion)
        result = _overlay_tool_projection(world, [req, completion])
        view = result.views["a-1"]
        assert "tool_completions" in view.components
        completions = view.components["tool_completions"]
        assert str(req.event_id) in completions

    def test_tick_is_preserved(self):
        """The projection is an overlay, not a fold
        step. The returned World's tick equals the
        input's tick (no clock advance)."""
        world = World(tick=42, storage=ArchetypeStorage(), views={})
        events = [_event(event_type="tool.requested", agent_id="a-1")]
        result = _overlay_tool_projection(world, events)
        assert result.tick == 42

    def test_named_tool_requested_installs_slot_with_name(self):
        """Regression: a ``tool.<name>.requested`` event
        (the canonical form emitted by
        ``ToolAwareSystem.request_tool``) installs the
        ``tool_requests`` slot AND captures the tool name
        from the event type's middle segment. Before
        this fix, only the legacy bare ``tool.requested``
        form was recognised and the request was silently
        dropped, breaking the ``is_pending`` /
        ``has_requested`` checks in real runs."""
        world = _empty_world()
        req = _event(event_type="tool.weather_api.requested", agent_id="a-1")
        world = world.with_event(req)
        result = _overlay_tool_projection(world, [req])
        view = result.views["a-1"]
        assert "tool_requests" in view.components
        tool_requests = view.components["tool_requests"]
        assert str(req.event_id) in tool_requests
        # The tool_name is captured from the event type,
        # NOT from event.data["tool"] (which is empty
        # for the canonical form).
        assert tool_requests[str(req.event_id)].tool_name == "weather_api"

    def test_named_tool_requested_completion_joins_via_causation(self):
        """End-to-end: a ``tool.<name>.requested`` event
        followed by a ``tool.<name>.completed`` event
        joined by ``causation_id`` produces both a
        ``ToolCallRequest`` (with the right tool name)
        and a ``ToolCallCompletion`` referencing the
        same request_event_id. This is the canonical
        WorkerManager round-trip and the scenario that
        was broken before the fix."""
        world = _empty_world()
        req = _event(event_type="tool.weather_api.requested", agent_id="a-1")
        completion = _event(
            event_type="tool.weather_api.completed",
            agent_id="a-1",
            causation_id=req.event_id,
        )
        world = world.with_event(req).with_event(completion)
        result = _overlay_tool_projection(world, [req, completion])
        view = result.views["a-1"]
        # ADR-044: the request is NOT evicted when
        # the request and completion are in the
        # same batch (the request was created in
        # this batch; it never came from
        # ``base_views``). The completion is added.
        assert view.components["tool_requests"][str(req.event_id)].tool_name == (
            "weather_api"
        )
        assert str(req.event_id) in view.components["tool_completions"]
        completion_obj = view.components["tool_completions"][str(req.event_id)]
        assert completion_obj.status == "completed"
        assert completion_obj.request_event_id == str(req.event_id)

    def test_request_remains_visible_until_completion_arrives_in_next_batch(self):
        """ADR-044: a request emitted in tick N remains
        visible in the ``tool_requests`` slot in tick
        N+1 (when the completion arrives). This is the
        multi-tick correlation that the chat round-trip
        (example 05b) depends on.

        Before the accumulation fix, the
        ``_overlay_tool_projection`` rebuilt the slot
        from the current batch only, losing pending
        requests from previous ticks. The system in
        tick N+1 had no way to match the completion
        to the request.

        The fix: ``overlay_tool_calls`` MERGES the
        new requests/completions with the existing
        slots on the base view (which carry the
        state from previous ticks), keyed by
        ``request_event_id``. The request is
        **evicted** only when a matching completion
        arrives in a *subsequent* batch (i.e. the
        request was carried in from
        ``base_views``).
        """
        # Tick N: a request lands.
        world_n = _empty_world()
        req = _event(event_type="tool.weather_api.requested", agent_id="a-1")
        world_n = world_n.with_event(req)
        world_n_with_overlay = _overlay_tool_projection(world_n, [req])
        assert (
            str(req.event_id)
            in (world_n_with_overlay.views["a-1"].components["tool_requests"])
        )

        # Tick N+1: a completion lands. The
        # dispatcher passes the WORLD from tick N
        # (with the request slot installed) as the
        # base; the overlay should preserve the
        # request slot AND add the completion. The
        # request is **evicted** (it was carried
        # from base_views; the system has reacted
        # or will react in this tick).
        completion = _event(
            event_type="tool.weather_api.completed",
            agent_id="a-1",
            causation_id=req.event_id,
        )
        world_n_plus_1 = world_n_with_overlay.with_event(completion)
        world_n_plus_1_with_overlay = _overlay_tool_projection(
            world_n_plus_1, [completion]
        )
        view = world_n_plus_1_with_overlay.views["a-1"]
        # The request is evicted (it was in
        # base_views; the matching completion has
        # now arrived).
        assert str(req.event_id) not in view.components["tool_requests"]
        # The completion is in the slot.
        assert str(req.event_id) in view.components["tool_completions"]
        assert (
            view.components["tool_completions"][str(req.event_id)].status == "completed"
        )

    def test_unrelated_request_persists_across_batches(self):
        """ADR-044: when batch 1 emits a request and
        batch 2 has an unrelated event (no completion
        for the request), the request is preserved.
        The system has not yet reacted; the request
        is still in flight.
        """
        # Tick N: a request lands.
        world_n = _empty_world()
        req = _event(event_type="tool.weather_api.requested", agent_id="a-1")
        world_n = world_n.with_event(req)
        world_n_with_overlay = _overlay_tool_projection(world_n, [req])

        # Tick N+1: an unrelated tool completion (a
        # different tool — no match for ``req``).
        # The dispatcher's overlay should be a no-op
        # for the request slot; the original request
        # is still in flight.
        unrelated_completion = _event(
            event_type="tool.calendar_api.completed", agent_id="a-1"
        )
        world_n_plus_1 = world_n_with_overlay.with_event(unrelated_completion)
        world_n_plus_1_with_overlay = _overlay_tool_projection(
            world_n_plus_1, [unrelated_completion]
        )
        view = world_n_plus_1_with_overlay.views["a-1"]
        # The request is still there (the unrelated
        # completion doesn't evict it).
        assert str(req.event_id) in view.components["tool_requests"]
        # The unrelated completion is an orphan (no
        # matching request); the framework drops it
        # from the completions slot. The request
        # remains in flight, exactly as the
        # dispatcher would see it.
        assert view.components["tool_completions"] == {}
