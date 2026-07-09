# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.memory — Vertical-specific memory patterns.

Re-exports the knowledge consolidation and solution
promotion pipeline. These are **vertical features** of
the agents product (they assume the existence of a Tool
registry, a PII tool, an embedding provider, and a
FalkorDB graph backend), not framework primitives.

The framework-level memory primitives (Session,
Profile, Continuity, CacheWarmer, Projector) live in
:mod:`kntgraph.memory`. They are the building
blocks; this package is a concrete composition of
them around the "knowledge graph" use case.

Why split
---------

The framework should be usable without a FalkorDB
backend or a PII redaction tool — those are
vertical-specific. Hosting the solution pipeline
under :mod:`kntgraph.memory` would force every
downstream consumer (even apps that only need the
Session cache) to know about the knowledge
consolidation's optional dependencies. The split
keeps the framework lean and lets vertical apps opt
in by adding :mod:`kntgraph.agents.memory` to their stack.

Iter 28 FU 8 (ADR-034): the `KnowledgeConsolidator`
god module is replaced by 3 Reactive Systems
(`SolutionExtractorSystem`, `SolutionPromoterSystem`,
`SolutionReviewPublisherSystem`). The Systems are
registered in the `ReactiveDispatcher` like any other
WorldSystem, not started in a standalone coroutine.
See :mod:`kntgraph.agents.memory.solution_extractor`,
:mod:`kntgraph.agents.memory.solution_promoter`,
:mod:`kntgraph.agents.memory.solution_review_publisher`.

See also
--------

* :mod:`kntgraph.memory` — the framework primitives.
* :mod:`kntgraph.agents.tools` — the Tool implementations
  consumed by the Solution promoter (LLM, PII, etc).
"""

from kntgraph.agents.memory.solution_extractor import SolutionExtractorSystem
from kntgraph.agents.memory.solution_promoter import (
    PromoteStats,
    SolutionPromoterSystem,
)
from kntgraph.agents.memory.solution_review_publisher import (
    SolutionReviewPublisherSystem,
)
from kntgraph.agents.memory.solutions import (
    SolutionCandidate,
    SolutionExtractor,
    SolutionPromotionBus,
    SolutionPromoter,
)


__all__ = [
    "PromoteStats",
    "SolutionCandidate",
    "SolutionExtractor",
    "SolutionExtractorSystem",
    "SolutionPromotionBus",
    "SolutionPromoter",
    "SolutionPromoterSystem",
    "SolutionReviewPublisherSystem",
]
