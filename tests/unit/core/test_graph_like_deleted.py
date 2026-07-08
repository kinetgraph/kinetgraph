# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression test: the ``GraphLike`` Protocol has been
**deleted** (Iter 28+follow-up+4). The framework
production ``GraphPool`` returns ``GraphAdapter``;
the dev-only ``LiteGraphPool`` also returns
``GraphAdapter``. No caller consumes the sync-only
``GraphLike`` shape.

This test is the deletion gate. If a future refactor
re-introduces ``GraphLike`` (or the docstring/comment
that references it), this test fails.

Background
----------

``GraphLike`` was introduced in Iter 24 (see
:mod:`kntgraph.ADRs.ADR-024`) as the sync handle
shape returned by ``LiteFalkorDBClient``. Iter 28
follow-up 2 (see
:mod:`kntgraph.ADRs.ADR-029`) migrated the
dev-only client to ``LiteGraphPool`` which returns
``GraphAdapter`` (the framework's current shape).
This test guards the deletion of the now-unused
``GraphLike`` Protocol.
"""

from __future__ import annotations

import pytest


class TestGraphLikeDeleted:
    """The ``GraphLike`` Protocol is GONE. All callers
    consume ``GraphAdapter`` (the async, canonical
    shape)."""

    def test_graph_like_not_exported_from_core_typing(self) -> None:
        """Importing ``GraphLike`` from
        ``kntgraph.core._typing`` must fail.

        Before this iter, ``GraphLike`` was a public
        Protocol re-exported in ``__all__``. After this
        iter, it is gone.
        """
        with pytest.raises(ImportError):
            from kntgraph.core._typing import GraphLike  # noqa: F401

    def test_graph_like_attribute_missing_from_module(self) -> None:
        """The module attribute ``GraphLike`` is gone
        (catches both ``from x import GraphLike`` and
        ``x.GraphLike`` access patterns)."""
        from kntgraph.core import _typing

        assert not hasattr(_typing, "GraphLike"), (
            "GraphLike should be deleted from kntgraph.core._typing"
        )

    def test_graph_like_not_in_typing_all(self) -> None:
        """``GraphLike`` is not in ``core._typing.__all__``."""
        from kntgraph.core import _typing

        assert "GraphLike" not in _typing.__all__

    def test_mapping_import_removed_too(self) -> None:
        """The ``Mapping`` import in ``core._typing``
        was only used by ``GraphLike.query``. The
        deletion of ``GraphLike`` makes ``Mapping``
        unused; it should be removed too."""
        from kntgraph.core import _typing

        # Mapping was a module-level import. If
        # removed, ``Mapping`` is not in the module's
        # namespace (under its original name).
        assert "Mapping" not in dir(_typing), (
            "Mapping should be removed from "
            "kntgraph.core._typing (it was only "
            "used by the deleted GraphLike.query)"
        )

    def test_graph_adapter_still_works(self) -> None:
        """The canonical ``GraphAdapter`` is still
        importable from the framework's knowledge
        graph module."""
        from kntgraph.knowledge.graph import GraphAdapter

        assert hasattr(GraphAdapter, "query")

    def test_lite_graph_client_still_works(self) -> None:
        """The dev-only ``LiteGraphPool`` still
        returns ``GraphAdapter`` (not the deleted
        ``GraphLike``)."""
        from kntgraph.infra.graph._lite_pool import (
            LiteGraphAdapter,
            LiteGraphPool,
        )
        from kntgraph.knowledge.graph import GraphAdapter

        # LiteGraphAdapter must subclass GraphAdapter
        # (proves the public surface is unchanged).
        assert issubclass(LiteGraphAdapter, GraphAdapter)
        assert hasattr(LiteGraphPool, "graph")
