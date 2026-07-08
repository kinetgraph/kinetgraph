# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph -- typed graph database boundary (Iter 10).

The package is the framework-level abstraction for any
graph database. The Protocol, the value types, and the
client facade live here; the concrete FalkorDB adapter
and the per-node sub-adapters live in sub-modules.

Public surface:

  - ``GraphAdapter``         -- Protocol, async-only.
  - ``GraphQueryResult``     -- immutable row container.
  - ``GraphError``           -- concrete error type.

Sub-adapters (``GraphAgentAdapter``, ``GraphDocumentAdapter``,
``GraphToolCallAdapter``, ``GraphSolutionAdapter``) live
under ``_sub/`` and are exported as the Iter 11-14
sharding work lands.
"""

from ._protocol import (
    GraphAdapter,
    GraphError,
    GraphQueryResult,
)

__all__ = [
    "GraphAdapter",
    "GraphError",
    "GraphQueryResult",
]
