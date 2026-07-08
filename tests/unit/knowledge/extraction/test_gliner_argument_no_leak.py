# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the architectural leak documented
in ADR-025 §4 (Iter 27).

Before Iter 27, the framework's
``SLMArgumentExtractor`` imported
``GlinerArgumentAdapter`` from the vertical
``kntgraph.agents.knowledge.argument_extractor`` package.
This violated AGENTS.md §1.2 ("framework never depends
on vertical").

Iter 27 moved ``GlinerArgumentAdapter`` to the
framework, and turned the vertical package's
``_gliner_wrapper.py`` into a re-export shim. The
follow-up iter deleted the shim entirely.

These tests verify that the framework's pieces are
self-contained and that the vertical can no longer be
the source of a leak (because the vertical doesn't
exist).
"""

from __future__ import annotations


class TestExtractionPackageNoLeak:
    """The framework's ``extraction`` package must
    never import from ``kntgraph.agents`` at module load
    time."""

    def test_vertical_package_does_not_exist(self):
        """Iter 28 follow-up: the vertical package is
        GONE. Importing it must fail."""
        import pytest

        with pytest.raises(ModuleNotFoundError):
            import kntgraph.agents.knowledge.argument_extractor  # noqa: F401  # pyright: ignore[reportMissingImports]

    def test_slm_argument_extractor_does_not_load_vertical(
        self,
    ):
        """``SLMArgumentExtractor`` (the facade) must
        not transitively load ``kntgraph.agents.knowledge``
        (the vertical) when an explicit ``adapter=``
        is supplied.

        Iter 28 follow-up: the vertical package is
        gone, so the only way the leak could happen is
        if a future refactor re-introduces the vertical.
        The subprocess below verifies the framework's
        facade works without loading anything from
        ``kntgraph.agents.knowledge`` (the only
        ``kntgraph.agents.knowledge.*`` module that exists
        today is ``solution_projector`` and the
        ``__init__``; neither is argument-related).

        Implementation note: this test runs in a
        subprocess to avoid ``sys.modules`` pollution.
        """
        import subprocess
        import sys
        import textwrap

        script = textwrap.dedent(
            """
            import sys

            from kntgraph.knowledge.extraction import (
                SLMArgumentExtractor,
            )
            from kntgraph.tools.registry import ToolRegistry

            class _StubAdapter:
                model_name = "stub"
                async def extract(self, text, tool_name):
                    return None

            reg = ToolRegistry()
            _ = SLMArgumentExtractor(
                reg, adapter=_StubAdapter()
            )

            # No kntgraph.agents.knowledge.argument_extractor
            # should be loadable (it was deleted).
            try:
                import kntgraph.agents.knowledge.argument_extractor  # noqa: F401  # pyright: ignore[reportMissingImports]
                print("LEAKED: vertical re-imported")
                sys.exit(1)
            except ModuleNotFoundError:
                print("OK: vertical deleted, no leak")
                sys.exit(0)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "PYTHONPATH": "kntgraph/src:kntgraph.agents/src",
            },
        )
        assert result.returncode == 0, (
            f"SLMArgumentExtractor leaked into "
            f"kntgraph.agents.knowledge.argument_extractor: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout


class TestGlinerArgumentAdapterFromFramework:
    """The framework's ``GlinerArgumentAdapter`` is a
    drop-in replacement for the vertical one. Tests
    verify the public surface (constructor signature,
    ``model_name`` property, ``extract`` method)
    matches the existing contract."""

    def test_module_path(self):
        """The adapter lives at
        ``kntgraph.knowledge.extraction.gliner_argument``
        (canonical) and is re-exported from
        ``kntgraph.knowledge.extraction``."""
        import kntgraph.knowledge.extraction as ext

        assert hasattr(ext, "GlinerArgumentAdapter")
        # The concrete module path:
        from kntgraph.knowledge.extraction import (
            gliner_argument as mod,
        )

        assert hasattr(mod, "GlinerArgumentAdapter")
        assert ext.GlinerArgumentAdapter is mod.GlinerArgumentAdapter

    def test_is_a_argument_extractor(self):
        """The adapter is IS-A ``ArgumentExtractor``
        (the framework-level Protocol)."""
        from kntgraph.knowledge.extraction import (
            GlinerArgumentAdapter,
        )
        from kntgraph.knowledge.extraction.base import (
            ArgumentExtractor,
        )

        # The class itself (not an instance, since
        # construction triggers model load).
        assert issubclass(GlinerArgumentAdapter, ArgumentExtractor)

    def test_resolve_model_name_helper(self):
        """The static ``_resolve_model_name`` helper
        returns the explicit arg when given, or the
        Settings default when ``None``."""
        from kntgraph.knowledge.extraction import (
            GlinerArgumentAdapter,
        )
        from kntgraph.infra.config import fresh_settings

        # Explicit arg wins.
        assert (
            GlinerArgumentAdapter._resolve_model_name("custom-model") == "custom-model"
        )

        # None reads from Settings.
        fresh_settings.cache_clear()
        result = GlinerArgumentAdapter._resolve_model_name(None)
        assert result == "gliner2-base"  # default
        fresh_settings.cache_clear()
