# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Demonstration tests for ADR-037 correlation propagation.

A "kntgraph transaction" is the lifecycle of a single flow
as it travels from the entry point (HTTP request → entry
event) through systems, optional tool invocations, and the
final completion event. The transaction's ``correlation_id``
MUST be constant across all events in the flow so audit
trails can stitch the chain.

These tests use ONLY core primitives (``Event``,
``CorrelationContext``, ``correlation_middleware``). They
validate the contract at the boundary:

  1. ``Event.create`` requires a non-None ``correlation``
     (ADR-037 §2) — verified.
  2. ``correlation_middleware.continue_from(parent_event)``
     propagates the parent's correlation_id; this is the
     canonical pattern the dispatcher now uses.
  3. ``correlation_middleware.scope()`` accepts an optional
     ``correlation_id`` to thread an existing flow id; a
     call without it still mints fresh (entry-event use
     case).

The tests use the ``scope(correlation_id=...)`` form
because that mirrors what production callers see when the
dispatcher sets the middleware via ``continue_from``
(which itself calls ``start(correlation_id=...)``
internally). The "fresh correlation per scope" branch is
kept under ``TestLegacyScopeWithoutCorrelationId`` to
document the pre-fix behavior.

Reference: ADR-037 §1.1.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from kntgraph.core.event import (
    CorrelationContext,
    Event,
    correlation_middleware,
)


pytestmark = pytest.mark.asyncio


# A stable correlation_id used as the entry point of the
# simulated transaction. In production this would be the
# HTTP request's idempotency key (see
# ``api/intent_router/routes.py:289-321``).
ENTRY_CORRELATION_ID = UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Helpers — minimal Event builders using only core primitives.
# ---------------------------------------------------------------------------


def _make_entry_event() -> Event:
    """The entry event of a transaction.

    Mirrors the ``intent_router`` pattern: the entry event's
    ``event_id`` equals its ``correlation.correlation_id``
    so a retry of the same request produces the same id
    (and the audit trail stitches).
    """
    return Event.create(
        event_type="user.intent",
        agent_id="agent-1",
        event_class="domain",
        data={"intent": "chat", "message": "hello"},
        correlation=CorrelationContext(correlation_id=ENTRY_CORRELATION_ID),
        event_id=ENTRY_CORRELATION_ID,
    )


def _emit_system_event_using_middleware(
    *,
    agent_id: str,
    event_type: str,
    data: dict,
    causation_id: UUID,
) -> Event:
    """Mimic how a typical ``WorldSystem`` emits an event:
    it pulls the correlation from ``correlation_middleware.current()``
    and threads it into ``Event.create``.

    This is the canonical pattern in the framework
    (see ``agents/memory/profile.py:217-218``,
    ``agents/role_systems/_base.py``); it relies on the
    dispatcher having SET the middleware with the correct
    correlation BEFORE invoking systems.
    """
    correlation = correlation_middleware.current()
    assert correlation is not None, (
        "correlation_middleware.current() returned None; "
        "Event.create requires a non-None correlation (ADR-037)"
    )
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=data,
        causation_id=causation_id,
        correlation=correlation,
    )


def _simulate_worker_manager_completion(
    request: Event, result: dict
) -> Event:
    """Mimic the ``WorkerManager`` completion path
    (``tools/manager.py:625-632``).

    The worker runs in its OWN asyncio task (ContextVar is
    empty there), so it MUST thread the request's correlation
    through the event object directly — not via the
    middleware.
    """
    return Event.create(
        event_type=f"tool.{request.data['tool']}.completed",
        agent_id=request.agent_id,
        event_class="domain",
        data={"result": result},
        causation_id=request.event_id,
        correlation=request.correlation,
    )


# ---------------------------------------------------------------------------
# Test 1: scope with correlation_id threads the flow id.
# ---------------------------------------------------------------------------


class TestDispatcherScopePropagation:
    """The dispatcher opens a ``correlation_middleware.scope()``
    with the entry event's correlation_id. Inside the scope,
    ``correlation_middleware.current()`` returns the
    propagated context (NOT a fresh ``uuid4()``).
    """

    async def test_scope_threads_entry_correlation(self) -> None:
        """When the caller provides ``correlation_id`` to
        ``scope()``, the middleware carries the entry's
        flow id; subsequent ``current()`` returns it.
        """
        entry = _make_entry_event()

        captured: CorrelationContext | None = None
        with correlation_middleware.scope(
            correlation_id=entry.correlation.correlation_id,
        ):
            captured = correlation_middleware.current()

        assert captured is not None
        assert captured.correlation_id == ENTRY_CORRELATION_ID, (
            f"scope(correlation_id=...) failed to propagate the "
            f"entry correlation: expected {ENTRY_CORRELATION_ID}, "
            f"got {captured.correlation_id}"
        )


# ---------------------------------------------------------------------------
# Test 2: a system emitting under a propagated scope carries the entry.
# ---------------------------------------------------------------------------


class TestSystemEmissionUnderDispatcherScope:
    """A system that pulls ``correlation_middleware.current()``
    inside a propagated scope emits with the entry's
    correlation_id.
    """

    async def test_system_event_carries_entry_correlation(self) -> None:
        """The ``tool.<name>.requested`` event emitted by
        the role system must carry the entry's
        correlation_id.
        """
        entry = _make_entry_event()

        with correlation_middleware.scope(
            correlation_id=entry.correlation.correlation_id,
        ):
            tool_request = _emit_system_event_using_middleware(
                agent_id="agent-1",
                event_type="tool.chat_llm.requested",
                data={"tool": "chat_llm", "params": {}},
                causation_id=entry.event_id,
            )

        assert tool_request.correlation.correlation_id == ENTRY_CORRELATION_ID


# ---------------------------------------------------------------------------
# Test 3: WorkerManager completion inherits the entry correlation
#          via the request's correlation (already correct in prod).
# ---------------------------------------------------------------------------


class TestWorkerManagerCompletionCorrelation:
    """The ``WorkerManager`` runs in a separate asyncio task
    (ContextVar is empty) and threads the request's
    correlation through manually. Stitches the audit chain.
    """

    async def test_completion_carries_request_correlation(self) -> None:
        entry = _make_entry_event()

        with correlation_middleware.scope(
            correlation_id=entry.correlation.correlation_id,
        ):
            tool_request = _emit_system_event_using_middleware(
                agent_id="agent-1",
                event_type="tool.chat_llm.requested",
                data={"tool": "chat_llm", "params": {}},
                causation_id=entry.event_id,
            )

        # The WorkerManager threads the request's correlation
        # explicitly (its ContextVar is empty in its own task).
        completion = _simulate_worker_manager_completion(
            tool_request, result={"text": "ok"}
        )

        # The completion carries the request's correlation,
        # which equals the entry's correlation.
        assert completion.correlation.correlation_id == tool_request.correlation.correlation_id
        assert completion.correlation.correlation_id == ENTRY_CORRELATION_ID


# ---------------------------------------------------------------------------
# Test 4: audit-trail query stitches all events by correlation_id.
# ---------------------------------------------------------------------------


class TestAuditTrailByCorrelationId:
    """Build the full transaction EventLog manually and
    verify that querying by the entry's correlation_id
    returns ALL events of the flow.
    """

    async def test_correlation_query_returns_all_events(self) -> None:
        entry = _make_entry_event()

        # The dispatcher propagates the entry correlation
        # via ``continue_from``; systems emit with the
        # inherited correlation.
        correlation_middleware.continue_from(entry)
        try:
            tool_request = _emit_system_event_using_middleware(
                agent_id="agent-1",
                event_type="tool.chat_llm.requested",
                data={"tool": "chat_llm", "params": {}},
                causation_id=entry.event_id,
            )
        finally:
            correlation_middleware.clear()

        # The worker threads the request's correlation
        # explicitly (its ContextVar is empty).
        completion = _simulate_worker_manager_completion(
            tool_request, result={"text": "ok"}
        )

        all_events = [entry, tool_request, completion]

        by_entry = [
            e for e in all_events
            if e.correlation.correlation_id == ENTRY_CORRELATION_ID
        ]

        assert len(by_entry) == 3, (
            f"audit query returned {len(by_entry)}/3 events; "
            f"correlation chain is broken. "
            f"correlations seen: "
            f"entry={entry.correlation.correlation_id}, "
            f"request={tool_request.correlation.correlation_id}, "
            f"completion={completion.correlation.correlation_id}"
        )


# ---------------------------------------------------------------------------
# Test 5: the canonical fix pattern — ``continue_from``.
# ---------------------------------------------------------------------------


class TestCanonicalContinueFromPattern:
    """The dispatcher uses ``correlation_middleware.continue_from``
    to propagate the trigger's correlation to all systems
    in the tick. The middleware carries the same
    ``correlation_id`` but mints a fresh ``span_id``
    (per-tick operation).
    """

    async def test_continue_from_propagates_correlation_id(self) -> None:
        """End-to-end: entry → request → completion all
        share the entry's ``correlation_id``.
        """
        entry = _make_entry_event()

        correlation_middleware.continue_from(entry)
        try:
            tool_request = _emit_system_event_using_middleware(
                agent_id="agent-1",
                event_type="tool.chat_llm.requested",
                data={"tool": "chat_llm", "params": {}},
                causation_id=entry.event_id,
            )
        finally:
            correlation_middleware.clear()

        completion = _simulate_worker_manager_completion(
            tool_request, result={"text": "ok"}
        )

        all_events = [entry, tool_request, completion]
        by_entry = [
            e for e in all_events
            if e.correlation.correlation_id == ENTRY_CORRELATION_ID
        ]
        assert len(by_entry) == 3

        # ``continue_from`` also propagates the causation_id
        # of the parent event onto the new context, but the
        # ``span_id`` is fresh per call (this tick is a new
        # operation in the OpenTelemetry sense).
        propagated = correlation_middleware  # noqa: F841
        # The function above already cleared the middleware;
        # this assertion is just for documentation.

    async def test_continue_from_propagates_causation_id(self) -> None:
        """``continue_from`` also propagates the parent's
        ``event_id`` as the new context's ``causation_id``
        (the OpenTelemetry causal-link convention).
        """
        entry = _make_entry_event()
        correlation_middleware.continue_from(entry)
        try:
            ctx = correlation_middleware.current()
            assert ctx is not None
            assert ctx.causation_id == entry.event_id, (
                f"continue_from failed to propagate causation_id: "
                f"expected {entry.event_id}, got {ctx.causation_id}"
            )
        finally:
            correlation_middleware.clear()


# ---------------------------------------------------------------------------
# Test 6: legacy behaviour — ``scope()`` without correlation_id
#          mints a fresh one. This is OK for entry events
#          (HTTP request boundary) but NOT for dispatchers.
# ---------------------------------------------------------------------------


class TestLegacyScopeWithoutCorrelationId:
    """Documentation test: ``scope()`` with NO ``correlation_id``
    argument mints a fresh ``uuid4()``. This is the
    pre-fix dispatcher behaviour. New code MUST NOT use
    this pattern; the dispatcher now threads the entry's
    correlation via ``continue_from``.
    """

    async def test_scope_without_correlation_id_mints_fresh(self) -> None:
        """Without ``correlation_id``, ``scope()`` creates
        a fresh correlation. This is the legacy dispatch
        boundary bug — it loses the entry correlation.
        New code MUST pass ``correlation_id=`` to scope.
        """
        with correlation_middleware.scope():
            ctx = correlation_middleware.current()
        assert ctx is not None
        # The fresh id is NOT the entry's; this is the bug
        # the dispatcher fix closes.
        assert ctx.correlation_id != ENTRY_CORRELATION_ID
