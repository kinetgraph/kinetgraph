# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression test: the vertical
``kntgraph.agents.knowledge.argument_extractor`` package
has been **deleted** (Iter 28+follow-up). Importing
it should raise ``ModuleNotFoundError`` (or
``ImportError``).

This test is the deletion gate. If a future refactor
re-introduces the vertical package, this test fails.

Iter 28 left the vertical as a re-export shim so
backward compat was preserved. The follow-up iter
that this test guards is the **deletion** of that
shim -- the package is no longer needed because all
callers have been migrated to the framework path.
"""

from __future__ import annotations

import pytest


class TestArgumentExtractorVerticalDeleted:
    """The vertical ``argument_extractor`` package is
    GONE. All callers consume the framework path
    directly."""

    def test_vertical_package_does_not_exist(self) -> None:
        """Importing the vertical package must fail
        with ModuleNotFoundError or ImportError.

        Before this iter, the package existed as a
        re-export shim of the framework's pieces.
        After this iter, it is gone.
        """
        with pytest.raises((ModuleNotFoundError, ImportError)):
            import kntgraph.agents.knowledge.argument_extractor  # noqa: F401  # pyright: ignore[reportMissingImports]

    def test_vertical_subpackage_does_not_exist(self) -> None:
        """The subpackages (``_finder``, ``_coerce``,
        etc.) are gone too."""
        for sub in (
            "_finder",
            "_coerce",
            "_extractor",
            "_gliner_finder",
            "_schema",
            "_gliner_wrapper",
        ):
            with pytest.raises((ModuleNotFoundError, ImportError)):
                __import__(
                    f"kntgraph.agents.knowledge.argument_extractor.{sub}",
                    fromlist=["*"],
                )

    def test_framework_path_still_works(self) -> None:
        """The framework's pieces are importable
        from the canonical path."""
        from kntgraph.knowledge.extraction.argument import (
            FieldFinder,
            RegexFieldFinder,
            SchemaArgumentExtractor,
            GlinerFieldFinder,
        )
        from kntgraph.knowledge.extraction import (
            GlinerArgumentAdapter,
        )
        from kntgraph.knowledge.extraction.argument._coerce import (
            coerce,
        )

        # Sanity: each is a class / callable.
        assert isinstance(FieldFinder, type)
        assert isinstance(RegexFieldFinder, type)
        assert isinstance(SchemaArgumentExtractor, type)
        assert isinstance(GlinerFieldFinder, type)
        assert isinstance(GlinerArgumentAdapter, type)
        assert callable(coerce)
