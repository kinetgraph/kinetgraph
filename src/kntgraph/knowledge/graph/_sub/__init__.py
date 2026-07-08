# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._sub -- domain-specific sub-adapters
(``GraphAgentAdapter``, ``GraphDocumentAdapter``,
``GraphToolCallAdapter``, ``GraphSolutionAdapter``).

A ``Graph*Adapter`` is a **composable sub-schema** that
projects a single domain slice of the tenant's graph.
Every sub-adapter follows the same template (see
[ADR-019 §2.4](../../../../../ADRs/ADR-019-Epilogo-Typed-Adapters.md)
and [ADR-024 §2.1](../../../../../ADRs/ADR-024-FalkorDBClient-GraphPool-Migration.md)):

  1. Cypher constants as class attributes
     (``CYPHER_UPSERT``, ``CYPHER_FIND_BY_ID``, ...).
  2. Composition via ``__init__(graph: GraphAdapter)``
     — never inheritance from the Protocol.
  3. Typed methods with keyword-only parameters and
     explicit return types.
  4. ``GraphError`` raised at the boundary, never
     swallowed.
  5. Tests with a mock ``GraphAdapter`` (no network,
     no fakeredis).

Two-dimensional abstraction
---------------------------

The framework's graph layer is structured along **two
orthogonal axes**:

  - **Backend axis** (``GraphAdapter`` Protocol):
    encapsulates the storage engine (FalkorDB today;
    Neo4j / Memgraph / etc. tomorrow). A swap means
    replacing the concrete ``FalkorDBGraphAdapter`` with
    another Protocol impl; sub-adapters are unchanged.

  - **Schema axis** (``Graph*Adapter`` sub-adapters):
    each sub-adapter owns one slice of the graph's
    domain model (Agent, Document, ToolCall, Solution).
    Multiple sub-adapters compose over the same
    ``GraphAdapter`` to project different sub-schemas
    of the same tenant graph without interference.

Example composition
-------------------

``FalkorDBProjector`` (in ``knowledge/falkordb/adapter.py``)
instantiates three sub-adapters over a single
``GraphAdapter`` and projects three sub-schemas of the
same tenant graph in one pass:

    graph = client.graph(tenant_id)  # GraphAdapter (Protocol)
    agents     = GraphAgentAdapter(graph)      # Agent nodes
    documents  = GraphDocumentAdapter(graph)   # Document nodes + MENTIONS
    tool_calls = GraphToolCallAdapter(graph)   # ToolCall nodes
    await agents.upsert(agent_id="a1", ...)
    await documents.link_to_agent(...)

``SolutionProjector`` (in ``kntgraph.agents/knowledge/``)
uses ``GraphSolutionAdapter`` for the Solution sub-graph
(Problem / Action / Outcome / Tool nodes + the
``SOLVED_BY`` / ``FAILED_WITH`` / ``ON_TOOL`` /
``PRODUCED`` edges) over the same ``GraphAdapter`` from
the same ``GraphPool``.

Why two axes
------------

Splitting along two axes keeps the boundary explicit:

  - **Backend swap** (FalkorDB → Neo4j) touches only
    ``_adapter.py``. Sub-adapters, projectors, and
    retriever are unchanged.
  - **New domain schema** (e.g. a ``GraphEntityAdapter``
    for named-entity knowledge) touches only
    ``_sub/_entity.py``. The backend, the
    ``GraphAdapter`` Protocol, and other sub-adapters
    are unchanged.

Adding a new sub-adapter
------------------------

  1. Create ``_sub/_<domain>.py`` with a class
     ``Graph<Domain>Adapter`` that follows the
     5-point template above.
  2. Export it from this ``__init__.py``.
  3. Add a unit test file under
     ``tests/unit/knowledge/graph/_sub/`` with a
     ``MockGraphAdapter`` to verify Cypher shape and
     parameter binding without a real backend.
"""

from __future__ import annotations

from ._agent import GraphAgentAdapter
from ._document import GraphDocumentAdapter
from ._solution import GraphSolutionAdapter
from ._tool_call import GraphToolCallAdapter


__all__ = [
    "GraphAgentAdapter",
    "GraphDocumentAdapter",
    "GraphSolutionAdapter",
    "GraphToolCallAdapter",
]
