# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
knowledge.graph._sub._solution -- ``GraphSolutionAdapter`` for
the Solution sub-graph.

Public API re-exports :class:`GraphSolutionAdapter` from
:mod:`kntgraph.knowledge.graph._sub._solution._adapter`.

The implementation is split across private modules to keep
each file under the 500-L guideline (AGENTS.md §3.1):

  - ``_adapter`` -- the public class (write + read API).
  - ``_read_filters`` -- read-path Cypher templates, the
    WHERE-clause builders, and the row mappers.
  - ``_row_helpers`` -- the typed row-extraction primitives
    used by the row mappers.

External imports of ``GraphSolutionAdapter`` are unchanged
(``from kntgraph.knowledge.graph._sub._solution import
GraphSolutionAdapter``).
"""

from __future__ import annotations

from ._adapter import GraphSolutionAdapter


__all__ = ["GraphSolutionAdapter"]
