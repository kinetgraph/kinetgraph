# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
knowledge.graph._protocol -- Vertical re-export of the framework's
graph Protocol module.

The three types (``GraphAdapter``, ``GraphError``,
``GraphQueryResult``) were relocated to
``src/kntgraph/infra/graph/_protocol.py`` -- the
framework's canonical home. The vertical re-exports
them so existing callers
(``from kntgraph.knowledge.graph._protocol import
GraphAdapter``) keep working without import-path
changes. The new canonical home is:

    from kntgraph.infra.graph._protocol import (
        GraphAdapter, GraphError, GraphQueryResult,
    )

See ``infra/graph/_protocol.py`` for the full
rationale, the Protocol definition, and the wire-format
contract (Cypher; ``params`` dict).
"""

from __future__ import annotations

from ...infra.graph._protocol import (
    GraphAdapter,
    GraphError,
    GraphQueryResult,
)

__all__ = [
    "GraphAdapter",
    "GraphError",
    "GraphQueryResult",
]
