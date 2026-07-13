# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.fold -- Pure fold of continuity events.

Two helpers:

  - `_fold_continuity_events(tenant_id, user_id,
    events)`: replays a stream of `Event` for one
    continuity and produces the latest
    `ContinuityState`. Returns `None` when no
    `continuity.created` event is present.

  - `_reset_after_clear(last_tools, last_entities,
    last_categories)`: clears the per-slot dicts
    after a `continuity.cleared` event (or at the
    first event AFTER a clear, which restarts the
    cycle). Mutates the dicts in place.

The fold is pure: it does not touch Redis, the
EventLog, or any global state. The manager uses it
in `_fold_from_log` (cache miss path) and tests
exercise it directly with synthetic event lists.

Why split from `cache_codec.py`? The fold is the
*contract* (events → state); the cache codec is the
*encoding* (state → Hash dict). They share the
`ContinuityState` shape but have nothing else in
common — splitting them keeps each pure module
small and single-purpose.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

from ...core.event import Event
from .state import ContinuityEventType, ContinuityState


def _reset_after_clear(
    last_tools: dict[str, str],
    last_entities: dict[str, str],
    last_categories: dict[str, str],
) -> None:
    """
    Reset the per-tenant/per-user dicts after a
    `continuity.cleared` event (or at the first event
    AFTER a clear, which restarts the cycle).

    Mutates the dicts in place. The caller is
    responsible for clearing the `cleared_at`
    timestamp (it lives outside the dicts because
    it is a scalar, not a dict).

    The three dicts are independent parameters
    rather than a `ContinuityState` because the
    fold builds them incrementally and the
    dataclass is only constructed at the end (the
    fold can return `None` for a degenerate event
    list with no `created` event).
    """
    last_tools.clear()
    last_entities.clear()
    last_categories.clear()


def _fold_continuity_events(
    tenant_id: str,
    user_id: str,
    events: Iterable[Event],
) -> Optional[ContinuityState]:
    """
    Pure fold of continuity events. Returns ``None`` if no
    ``continuity.created`` event is present.

    After a ``continuity.cleared`` event, the dicts are
    reset to empty and ``cleared_at`` is set. Events arriving
    AFTER ``cleared`` are still recorded (the system may
    legitimately start a new continuity cycle), but they
    populate the state from scratch.

    ``created_at`` and ``updated_at`` come from the Event's
    ``timestamp`` (not from ``data``, which is
    idempotency-stable).
    """
    state = _ContinuityFoldState()
    for e in events:
        _apply_continuity_event(state, e)
    if state.created_at is None:
        return None
    return state.to_value(tenant_id, user_id)


class _ContinuityFoldState:
    """Mutable fold accumulator. Lives only inside
    ``_fold_continuity_events``; tests cover the
    public function, not this internal helper.
    """

    __slots__ = (
        "created_at",
        "updated_at",
        "cleared_at",
        "last_tools",
        "last_entities",
        "last_categories",
    )

    def __init__(self) -> None:
        self.created_at: Optional[float] = None
        self.updated_at: float = 0.0
        self.cleared_at: Optional[float] = None
        self.last_tools: dict[str, str] = {}
        self.last_entities: dict[str, str] = {}
        self.last_categories: dict[str, str] = {}

    def to_value(self, tenant_id: str, user_id: str) -> ContinuityState:
        return ContinuityState(
            tenant_id=tenant_id,
            user_id=user_id,
            last_tools=self.last_tools,
            last_entities=self.last_entities,
            last_categories=self.last_categories,
            created_at=self.created_at or 0.0,
            updated_at=self.updated_at,
            cleared_at=self.cleared_at,
        )


def _apply_continuity_event(state: _ContinuityFoldState, e: Event) -> None:
    """Dispatch one event into the fold state.

    Each event type has a dedicated handler in
    ``_HANDLERS``; the handler returns ``True`` when the
    event touches ``updated_at``. The dispatch is
    branch-free: a single dict lookup + call.
    """
    handler = _HANDLERS.get(e.event_type)
    if handler is None:
        return
    handler(state, e)


def _handle_created(state: _ContinuityFoldState, e: Event) -> None:
    ts = e.timestamp.timestamp()
    state.created_at = ts
    state.updated_at = ts


def _handle_tool_used(state: _ContinuityFoldState, e: Event) -> None:
    _maybe_resume_after_clear(state)
    ts = e.timestamp.timestamp()
    tool = str(e.data.get("tool", ""))
    if tool:
        state.last_tools[tool] = (
            f"{e.data.get('result_signature', '')}|{e.data.get('latency_ms', 0)}|{ts}"
        )
    state.updated_at = ts


def _handle_entity_seen(state: _ContinuityFoldState, e: Event) -> None:
    _maybe_resume_after_clear(state)
    ts = e.timestamp.timestamp()
    kind = str(e.data.get("kind", ""))
    value_hash = str(e.data.get("value_hash", ""))
    if kind and value_hash:
        state.last_entities[f"{kind}:{value_hash}"] = str(ts)
    state.updated_at = ts


def _handle_category_chosen(state: _ContinuityFoldState, e: Event) -> None:
    _maybe_resume_after_clear(state)
    ts = e.timestamp.timestamp()
    slot = str(e.data.get("slot", ""))
    value = str(e.data.get("value", ""))
    if slot:
        state.last_categories[slot] = f"{value}|{ts}"
    state.updated_at = ts


def _handle_cleared(state: _ContinuityFoldState, e: Event) -> None:
    _reset_after_clear(state.last_tools, state.last_entities, state.last_categories)
    state.cleared_at = e.timestamp.timestamp()
    state.updated_at = state.cleared_at


def _maybe_resume_after_clear(state: _ContinuityFoldState) -> None:
    """If a clear is in effect, drop the marker and reset
    the dicts so the new event populates them from
    scratch.
    """
    if state.cleared_at is None:
        return
    _reset_after_clear(state.last_tools, state.last_entities, state.last_categories)
    state.cleared_at = None


_HANDLERS = {
    ContinuityEventType.CREATED: _handle_created,
    ContinuityEventType.TOOL_USED: _handle_tool_used,
    ContinuityEventType.ENTITY_SEEN: _handle_entity_seen,
    ContinuityEventType.CATEGORY_CHOSEN: _handle_category_chosen,
    ContinuityEventType.CLEARED: _handle_cleared,
}


__all__ = ["_fold_continuity_events", "_reset_after_clear"]
