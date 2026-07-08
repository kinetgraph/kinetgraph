# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Solutions — the Solution tier of memory (ADR-010).

The Solution tier is **write-only knowledge** in FalkorDB
that captures reusable tool-call patterns. It is built
from the EventLog by:

  1. :class:`SolutionExtractor` (pure) walks a flat
     event list and turns each ``tool.*.requested`` /
     ``.completed`` / ``.failed`` pair into a
     :class:`SolutionCandidate` (Problem + Action +
     Outcome triple).

  2. :class:`SolutionPromotionBus` (FIFO) carries the
     candidates from the extractor to the promoter.

  3. :class:`SolutionPromoter` (adapter, side-effecting)
     takes each candidate, passes the tool data through
     ``PiiRedactionTool`` (Fase 3), and ``MERGE``s the
     Problem / Action / Outcome / Tool nodes and edges
     in FalkorDB.

In Fase 2 the promoter is a **skeleton** — it logs the
shape of what it would persist and counts by ``tool_name``.
The real Cypher ``MERGE`` and the PII gate land in Fase 3.
The skeleton exists so that:

  - The pure extractor is exercised end-to-end without
    FalkorDB.
  - The Consolidator wiring (Fase 2.5) can be tested
    against a no-op promoter.
  - The interface (``upsert_solution(candidate) -> int``)
    is stable from Fase 2 to Fase 3.

Idempotency
-----------

``SolutionPromoter.upsert_solution`` is called with the
same ``(request_event_id, params_fingerprint)`` pair on
replay (the EventLog is idempotent on event_id; replaying
the extractor produces the same ``SolutionCandidate``).
Fase 3 implements the ``MERGE`` so that repeated calls
collapse to the same graph state. The skeleton counts
"would-have-upserted" and reports it back to the caller
for assertion in tests.

Confidence bump
---------------

``SolutionExtractor.bump_confidence(candidates, events)``
is a pure function that scans the event history for the
same ``(problem_fingerprint, params_fingerprint)`` pair
appearing in N different agents. The bump is cross-agent
(ADR-010 Fase 4 — we ship the data path in Fase 2 because
the extractor is the natural owner). The threshold is the
bump step (default 2: seen in 2+ agents → +1 confidence).

This package does not import FalkorDB or Redis. The
Fase 3 promoter imports the FalkorDB adapter.

Package layout
--------------

* ``_values`` — frozen dataclasses (Problem, Action,
  Outcome, SolutionCandidate, PromoteStats).
* ``_fingerprints`` — pure hashing/parsing helpers
  (``fingerprint_problem``, ``fingerprint_params``,
  ``result_signature``, ``params_from_requested``,
  ``maybe_float``).
* ``_extractor_helpers`` — internal helpers
  (``_entities_to_tags``, ``_looks_like_cnpj``).
* ``_extractor`` — :class:`SolutionExtractor` (pure
  event history → list of candidates).
* ``_bus`` — :class:`SolutionPromotionBus` (FIFO queue).
* ``_promoter_helpers`` — internal ``redact_candidate``
  PII-gate logic.
* ``_promoter`` — :class:`SolutionPromoter` (adapter).

The ``ToolDescriptor`` dataclass lives in
:mod:`kntgraph.agents.tools.descriptors` (vertical-owned
shape decision). It is re-exported from here so the
Solution pipeline can be imported as a single namespace.

Iter 28 FU 8 (post-Iter 26): the legacy
``_compat.py`` module (with the
``is_tool_requested`` / ``is_tool_completed`` /
``is_tool_failed`` / ``tool_name_from_event_type``
wrappers) is **deleted**. The canonical helpers
live in :mod:`kntgraph.core.tool_event`:
``is_tool_event``, ``tool_name_of``,
``parse_tool_event``. Callers should import from
the canonical location. The behavior is preserved
(``tool_name_of`` returns ``None`` for non-tool
events; the legacy ``tool_name_from_event_type``
that raised ``ValueError`` is gone).
"""

from __future__ import annotations

from kntgraph.agents.memory.solutions._bus import SolutionPromotionBus
from kntgraph.agents.memory.solutions._extractor import SolutionExtractor
from kntgraph.agents.memory.solutions._fingerprints import (
    fingerprint_params,
    fingerprint_problem,
    params_from_requested,
    result_signature,
)
from kntgraph.agents.memory.solutions._promoter import SolutionPromoter
from kntgraph.agents.memory.solutions._values import (
    Action,
    Outcome,
    Problem,
    PromoteStats,
    SolutionCandidate,
)
from kntgraph.tools.descriptors import ToolDescriptor

__all__ = [
    # Value objects
    "ToolDescriptor",
    "Problem",
    "Action",
    "Outcome",
    "SolutionCandidate",
    "PromoteStats",
    # Pure helpers
    "fingerprint_problem",
    "fingerprint_params",
    "result_signature",
    "params_from_requested",
    # Components
    "SolutionExtractor",
    "SolutionPromotionBus",
    "SolutionPromoter",
]  # type: ignore[list-item]  # PromoteStats re-exported below
