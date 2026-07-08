# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.knowledge -- Vertical knowledge-graph adapters.

Re-exports the :class:`SolutionProjector` (the FalkorDB
adapter for the Solution sub-graph, ADR-010 §3). The
framework exposes only the generic knowledge primitives
(FalkorDB client, embedding provider Protocol); the
Solution-specific adapter lives here because the schema
is a vertical product choice.
"""

from kntgraph.agents.knowledge.solution_projector import SolutionProjector


__all__ = ["SolutionProjector"]
