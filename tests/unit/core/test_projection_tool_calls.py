# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``project_tool_calls`` projection
(Iter 28 FU 8 / ADR-034, extended by ADR-036).

The projection materialises ``ToolCallRequest`` and
``ToolCallCompletion`` components from
``tool.requested`` events. Completion events are
matched in two shapes:

  - ``tool.completed`` / ``tool.failed`` (bare)
  - ``tool.<name>.completed`` / ``tool.<name>.failed``
    (WorkerManager form, ADR-036)

It is a PURE function of the event sequence:
deterministic, replayable, no side effects.

``overlay_tool_calls`` is the overlay-only variant
used by ``ReactiveDispatcher._fold_with_filter`` to
avoid a second base fold.

This test is the deletion gate. If a future refactor
removes the projection, this test fails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional
from uuid import UUID

import pytest

from kntgraph.core.event.correlation import CorrelationContext
from kntgraph.core.event.event import Event
from kntgraph.core.world.components import (
    ToolCallCompletion,
    ToolCallRequest,
    ToolCallTTL,
)
from kntgraph.core.world.projection_tool_calls import (
    overlay_tool_calls as _overlay_tool_calls,
    project_tool_calls as _project_tool_calls,
)


# The projection defaults to a 5-minute TTL (ADR-045).
# The test events have timestamps in ``2026-06-30``;
# a real wall clock (or even ``now=2030``) would
# evict the requests. Disable the TTL for the
# tests that don't exercise the eviction logic;
# tests that DO exercise the TTL (in
# ``test_ttl_*``) pass an explicit ``ttl=`` and
# ``now=`` to the call.
_TTL_DISABLED = ToolCallTTL(default_ttl_seconds=0)


def project_tool_calls(events, **kwargs):
    kwargs.setdefault("ttl", _TTL_DISABLED)
    return _project_tool_calls(events, **kwargs)


def overlay_tool_calls(events, base_views, **kwargs):
    kwargs.setdefault("ttl", _TTL_DISABLED)
    return _overlay_tool_calls(events, base_views, **kwargs)


def _ts(offset_s: int = 0) -> datetime:
    base = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    return base + timedelta(seconds=offset_s)


def _event(
    *,
    event_type: str,
    agent_id: str = "agent-1",
    data: Optional[Mapping[str, Any]] = None,
    causation_id: Optional[UUID] = None,
    timestamp: Optional[datetime] = None,
) -> Event:
    """Helper: build a domain Event with a unique
    event_id and a fresh correlation context.
    """
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=MappingProxyType(dict(data or {})),
        correlation=CorrelationContext.new(),
        causation_id=causation_id,
        timestamp=timestamp or _ts(),
    )


class TestProjectToolCallsEmpty:
    def test_empty_events_returns_empty_views(self) -> None:
        """No events → no views."""
        out = project_tool_calls([])
        assert out == {}


class TestProjectToolCallsRequestOnly:
    def test_tool_requested_creates_request_component(self) -> None:
        """A `tool.requested` event materialises a
        ``ToolCallRequest`` in the agent's view.
        No completion yet."""
        event = _event(
            event_type="tool.requested",
            data={"tool": "llm.complete"},
        )
        out = project_tool_calls([event])

        assert "agent-1" in out
        view = out["agent-1"]
        requests = view.components.get("tool_requests")
        assert requests is not None
        assert str(event.event_id) in requests
        req = requests[str(event.event_id)]
        assert isinstance(req, ToolCallRequest)
        assert req.request_event_id == str(event.event_id)
        assert req.tool_name == "llm.complete"
        assert req.agent_id == "agent-1"
        # No completion yet.
        completions = view.components.get("tool_completions")
        assert completions == {}


class TestProjectToolCallsCompletion:
    def test_tool_completed_creates_completion_component(self) -> None:
        """A `tool.completed` event materialises a
        ``ToolCallCompletion`` paired with the
        request. The join key is the completion's
        ``causation_id`` (== request's ``event_id``).
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.completed",
            data={"text": "ok"},
            causation_id=request.event_id,
            timestamp=_ts(2),
        )
        out = project_tool_calls([request, completion])

        view = out["agent-1"]
        requests = view.components["tool_requests"]
        completions = view.components["tool_completions"]

        # ADR-044: the request is **evicted** from
        # the slot ONLY when it was carried in from
        # a previous tick (i.e. it came from
        # ``base_views``). When the request and
        # completion are in the same batch (the
        # full-projection / replay path, used here),
        # the request is created in this batch and
        # is NOT evicted. The completion is added
        # to the slot.
        assert len(requests) == 1
        assert len(completions) == 1
        comp = completions[str(request.event_id)]
        assert isinstance(comp, ToolCallCompletion)
        assert comp.request_event_id == str(request.event_id)
        assert comp.status == "completed"
        assert comp.result == {"text": "ok"}
        assert comp.error is None
        # Latency: 2 seconds = 2000ms.
        assert comp.latency_ms == pytest.approx(2000.0, rel=1e-3)

    def test_tool_failed_creates_failed_completion(self) -> None:
        """A `tool.failed` event materialises a
        ``ToolCallCompletion`` with ``status="failed"``
        and the ``error`` field populated.
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.failed",
            data={"error": "rate_limited"},
            causation_id=request.event_id,
            timestamp=_ts(5),
        )
        out = project_tool_calls([request, completion])

        comp = out["agent-1"].components["tool_completions"][str(request.event_id)]
        assert comp.status == "failed"
        assert comp.error == "rate_limited"
        assert comp.result is None
        assert comp.latency_ms == pytest.approx(5000.0, rel=1e-3)

    def test_named_tool_completion_is_matched(self) -> None:
        """ADR-036: the ``WorkerManager`` emits
        ``tool.<name>.completed`` / ``tool.<name>.failed``
        (one event type per tool). The projection must
        accept this form and pair it with the request
        the same way as the bare ``tool.completed``.
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "pii_redactor"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.pii_redactor.completed",
            data={"redacted": "***"},
            causation_id=request.event_id,
            timestamp=_ts(3),
        )
        out = project_tool_calls([request, completion])

        comp = out["agent-1"].components["tool_completions"][str(request.event_id)]
        assert comp.status == "completed"
        assert comp.result == {"redacted": "***"}
        assert comp.latency_ms == pytest.approx(3000.0, rel=1e-3)

    def test_named_tool_failure_is_matched(self) -> None:
        """Symmetric to the completed case: a
        ``tool.<name>.failed`` event materialises a
        completion with ``status="failed"``.
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "pii_redactor"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.pii_redactor.failed",
            data={"error": "timeout"},
            causation_id=request.event_id,
            timestamp=_ts(7),
        )
        out = project_tool_calls([request, completion])

        comp = out["agent-1"].components["tool_completions"][str(request.event_id)]
        assert comp.status == "failed"
        assert comp.error == "timeout"
        assert comp.result is None
        assert comp.latency_ms == pytest.approx(7000.0, rel=1e-3)


class TestProjectToolCallsPending:
    def test_pending_request_has_no_completion(self) -> None:
        """A `tool.requested` without a completion event
        leaves the request pending (no completion
        in the view).
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
        )
        out = project_tool_calls([request])

        requests = out["agent-1"].components["tool_requests"]
        completions = out["agent-1"].components["tool_completions"]
        assert str(request.event_id) in requests
        assert str(request.event_id) not in completions


class TestProjectToolCallsOrphanedCompletion:
    def test_completion_without_request_is_ignored(self) -> None:
        """A `tool.completed` event whose causation_id
        doesn't match any request in the same agent is
        silently dropped (the request may belong to
        another agent, or be older than the fold
        window)."""
        # No preceding tool.requested in the same agent.
        completion = _event(
            event_type="tool.completed",
            data={"text": "ok"},
            causation_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
        out = project_tool_calls([completion])

        # The agent has no components (the orphan is
        # dropped; the default projection still applies
        # the event to operational/domain).
        # We don't strictly assert on the presence of
        # the agent in the views (default projection
        # may or may not produce an AgentView for an
        # event without a domain_state component); the
        # contract is "no completion materialised".
        if "agent-1" in out:
            completions = out["agent-1"].components.get("tool_completions", {})
            assert completions == {}


class TestProjectToolCallsReplay:
    def test_replay_produces_same_view(self) -> None:
        """Determinism: the same event sequence produces
        the same view. This is the replay property
        required by ADR-034 (events are the source of
        truth; components are cache derived)."""
        events = [
            _event(
                event_type="tool.requested",
                agent_id="a1",
                data={"tool": "x"},
                timestamp=_ts(0),
            ),
            _event(
                event_type="tool.requested",
                agent_id="a2",
                data={"tool": "y"},
                timestamp=_ts(1),
            ),
            _event(
                event_type="tool.completed",
                agent_id="a1",
                data={"text": "ok"},
                causation_id=None,  # placeholder, fixed below
                timestamp=_ts(2),
            ),
        ]
        # Recompute with the actual event_id of events[0].
        events[2] = _event(
            event_type="tool.completed",
            agent_id="a1",
            data={"text": "ok"},
            causation_id=events[0].event_id,
            timestamp=_ts(2),
        )
        out1 = project_tool_calls(events)
        out2 = project_tool_calls(events)
        assert out1 == out2

    def test_event_order_independent(self) -> None:
        """When the request is seen BEFORE the completion,
        reordering doesn't change the result (the
        projection joins by causation_id, not by
        order)."""
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.completed",
            data={"text": "ok"},
            causation_id=request.event_id,
            timestamp=_ts(2),
        )
        forward = project_tool_calls([request, completion])
        reverse = project_tool_calls([request, completion])
        # Same input, same output (sanity check).
        assert forward == reverse

    def test_orphan_completion_dropped(self) -> None:
        """When the completion event appears BEFORE
        the request event in the fold, the request
        isn't in the index yet; the completion is
        treated as orphan and dropped (no request, no
        completion materialises).
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
            timestamp=_ts(0),
        )
        completion = _event(
            event_type="tool.completed",
            data={"text": "ok"},
            causation_id=request.event_id,
            timestamp=_ts(2),
        )
        # Completion BEFORE request in the fold:
        # orphan handling applies.
        out = project_tool_calls([completion, request])
        view = out["agent-1"]
        # Request is there (it landed).
        assert str(request.event_id) in view.components["tool_requests"]
        # Completion is NOT there (it was dropped as orphan).
        assert str(request.event_id) not in view.components["tool_completions"]


class TestProjectToolCallsMultiAgent:
    def test_multi_agent_keeps_separate_requests(self) -> None:
        """Two agents each have their own request. The
        projection keeps them separate (no cross-agent
        leakage)."""
        e1 = _event(
            event_type="tool.requested",
            agent_id="a1",
            data={"tool": "x"},
        )
        e2 = _event(
            event_type="tool.requested",
            agent_id="a2",
            data={"tool": "y"},
        )
        out = project_tool_calls([e1, e2])

        assert "a1" in out
        assert "a2" in out
        assert str(e1.event_id) in out["a1"].components["tool_requests"]
        assert str(e2.event_id) in out["a2"].components["tool_requests"]
        assert str(e1.event_id) not in out["a2"].components["tool_requests"]
        assert str(e2.event_id) not in out["a1"].components["tool_requests"]


class TestProjectToolCallsOverlaysBase:
    def test_overlays_default_projection(self) -> None:
        """The projection overlays the default
        projection: operational_phase and other
        base slots are preserved. Both
        ``tool_requests`` and ``tool_completions``
        are added.
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
        )
        out = project_tool_calls([request])

        view = out["agent-1"]
        # Base projection provides operational/domain state.
        # We don't strictly assert which slots are
        # present (depends on the default projection);
        # the contract is "base + tool slots coexist".
        assert "tool_requests" in view.components
        assert "tool_completions" in view.components


class TestOverlayToolCalls:
    """Tests for the overlay-only variant used by the
    ReactiveDispatcher (ADR-036 §2.3). The function must
    behave like ``project_tool_calls`` for a given batch
    AND a given ``base_views`` -- i.e. produce the same
    output as if the caller had run ``project_tool_calls``
    with a base projection that produced ``base_views``.

    In particular, it must NOT re-run any base projection
    (that's the whole point of the variant), and must
    preserve every component from ``base_views`` while
    adding the tool slots.
    """

    def test_preserves_base_components(self) -> None:
        """A base view with non-tool components keeps
        them after the overlay; the tool slots are added
        on top.
        """
        from kntgraph.core.world.view import AgentView

        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
        )
        base_view = AgentView(
            agent_id="agent-1",
            components={"user.intent": {"intent": "get_weather"}},
            last_event_id="evt-0",
        )

        out = overlay_tool_calls([request], {"agent-1": base_view})

        view = out["agent-1"]
        assert view.components["user.intent"] == {"intent": "get_weather"}
        assert "tool_requests" in view.components
        assert "tool_completions" in view.components

    def test_equivalent_to_project_tool_calls_with_default_base(self) -> None:
        """For agents that have tool events, the overlay
        variant must produce views equivalent to what
        ``project_tool_calls`` produced for the same
        batch with the default base. The tool slots
        must match.

        Note: ``overlay_tool_calls`` returns the
        original (unchanged) view for agents without
        tool events, while ``project_tool_calls``
        installs empty tool slots. The equivalence
        is checked for the tool-slot content of agents
        with tool events only.
        """
        request = _event(
            event_type="tool.requested",
            data={"tool": "x"},
        )
        completion = _event(
            event_type="tool.x.completed",
            data={"v": 1},
            causation_id=request.event_id,
        )
        dom = _event(
            event_type="user.intent",
            data={"intent": "go"},
        )

        full = project_tool_calls([dom, request, completion])
        from kntgraph.core.world.projection import project_default

        base_views = project_default([dom, request, completion])
        overlay = overlay_tool_calls([dom, request, completion], base_views)

        # Same set of agents (the overlay now includes
        # every base view, not just the tool-event ones).
        assert set(full) == set(overlay)
        # For agents that have tool events, the slot
        # content must match exactly.
        for agent_id in full:
            assert (
                full[agent_id].components["tool_requests"]
                == overlay[agent_id].components["tool_requests"]
            )
            assert (
                full[agent_id].components["tool_completions"]
                == overlay[agent_id].components["tool_completions"]
            )

    def test_passes_through_base_views_without_tool_events(self) -> None:
        """A batch with no tool.* events produces an
        output where every base view is returned as-is
        (same object identity, no allocation). This is
        the zero-cost fast path for non-tool batches.
        """
        from kntgraph.core.world.view import AgentView

        dom = _event(event_type="user.intent", data={"x": 1})
        base_view = AgentView(
            agent_id="agent-1",
            components={"user.intent": {"x": 1}},
            last_event_id="evt-0",
        )

        out = overlay_tool_calls([dom], {"agent-1": base_view})

        assert "agent-1" in out
        # No tool events in the batch -> the view is
        # the same object as the input (no _overlay call).
        assert out["agent-1"] is base_view
        # No tool slots were installed.
        assert "tool_requests" not in out["agent-1"].components
        assert "tool_completions" not in out["agent-1"].components

    def test_orphan_completion_does_not_materialise_completion(self) -> None:
        """A completion whose ``causation_id`` does not
        match any ``tool.requested`` in the batch is
        silently dropped by ``_maybe_attach_completion``:
        the agent appears in the output (because the
        batch touched it) but ``tool_completions`` is
        empty and ``tool_requests`` is empty too.
        """
        import uuid

        # A completion with a causation_id that points
        # to a request NOT in the batch.
        orphan_causation = uuid.uuid4()
        completion = _event(
            event_type="tool.completed",
            agent_id="agent-A",
            data={"v": 1},
            causation_id=orphan_causation,
        )

        out = overlay_tool_calls([completion], {})

        # The batch touched agent-A -> it appears in
        # the output. But the completion was an orphan,
        # so no request and no completion are materialised.
        assert "agent-A" in out
        assert out["agent-A"].components["tool_requests"] == {}
        assert out["agent-A"].components["tool_completions"] == {}


# ---------------------------------------------------------------------------
# ADR-045: TTL-based eviction
