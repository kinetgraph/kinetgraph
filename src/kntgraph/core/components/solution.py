# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.components.solution -- Canonical ``CachedSolution`` type.

Per the dependency rule (AGENTS.md §1.2, ADR-019 §2.1):
framework never imports from vertical. The
``CachedSolution`` value type was previously defined
in the vertical
(``src/kntgraph/agents/memory/solution_lookup.py``)
and imported by the framework's Redis adapter
(``src/kntgraph/infra/redis/_memory/_solution.py``)
-- a one-way vertical leak. The same pattern that
moved ``Event`` to ``core/event/event.py``,
``IdempotencyConflict`` to ``infra/redis/_errors.py``,
and ``Result`` to ``core/result/result.py`` now
moves ``CachedSolution`` here.

The vertical (``agents.memory.solution_lookup``)
**re-exports** ``CachedSolution`` from this module so
existing callers do not need to update their imports:

    from kntgraph.agents.memory.solution_lookup import CachedSolution

still works. The new canonical home is:

    from kntgraph.core.components.solution import CachedSolution

The framework's Redis adapter uses the new home
directly (per ADR-077 §3.4).

The shape: ``CachedSolution`` is the minimum payload
the lookup system needs to synthesise a
``tool.<name>.completed`` event. It is a frozen
dataclass; equality is structural; it is safe to
share across async boundaries (ADR-005 §3). The
``result`` field is the ``Mapping[str, JsonValue]``
the lookup pipeline emits to ``tool.<name>.completed``
events (the same shape the EventLog carries in
``Event.data`` -- per AGENTS.md §1.1 the framework's
``JsonValue`` is the only JSON-shape union).
"""

from __future__ import annotations

from dataclasses import dataclass

from .._typing import JsonValue

__all__ = ["CachedSolution"]


@dataclass(frozen=True, slots=True)
class CachedSolution:
    """
    The minimum payload the lookup system needs to
    synthesise a ``tool.<name>.completed`` event.

    Equivalent to a FalkorDB
    ``(:Action)-[:PRODUCED]->(:Outcome)`` edge plus the
    cached result body. Operators may extend this with
    the full ``Outcome`` (latency_ms, error_message,
    etc.) when wiring their own store.

    The ``result`` field is ``Mapping[str, JsonValue]``
    -- the framework's JSON-shape union (AGENTS.md
    §1.1). The lookup pipeline emits this exact shape
    to ``tool.<name>.completed`` events, so the
    cached result can be replayed verbatim into the
    EventLog without further conversion.
    """

    tool_name: str
    params_fingerprint: str
    confidence: int
    result: dict[str, JsonValue]
    # The EventLog ``event_id`` of the original
    # ``tool.<name>.completed`` event whose payload
    # this Solution captures. Used as the
    # ``request_event_id`` join key for downstream
    # consumers (the read-side Solution carries the
    # original completion's event id, not a new one).
    source_completion_event_id: str = ""
