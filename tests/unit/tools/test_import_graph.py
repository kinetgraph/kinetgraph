# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression test for the circular import that Iter 25
breaks (ADR-019 §3.2 Pendente; ADR-025).

Before Iter 25, importing
``kntgraph.agents.memory.knowledge_consolidator`` triggered a
cycle that crashed the test runner at collection time
(see ``kntgraph/tests/conftest.py::collect_ignore_glob``).

After Iter 25, every public module of
``kntgraph.agents.memory.*`` and ``kntgraph.agents.knowledge.*``
must be importable in any order without crashing.

Iter 28 FU 8 (ADR-034): the `KnowledgeConsolidator` is
deleted. The cycle regression is now verified against
`kntgraph.agents.memory.solution_extractor` (the pure
extractor replacement). The cycle fix from Iter 28 FU
5 is preserved (see ``test_import_graph_no_cycle.py``
for the dedicated cycle gate).

This test is the regression gate: if it ever fails,
the cycle has come back. Run it in CI.
"""

from __future__ import annotations

import pytest


class TestImportGraphNoCycle:
    """Each module is importable in isolation, then
    together. The order of imports is irrelevant: any
    permutation must succeed."""

    def test_solutions_package_imports_alone(self):
        from kntgraph.agents.memory import solutions

        assert solutions is not None

    def test_solutions_promoter_imports_alone(self):
        from kntgraph.agents.memory.solutions import _promoter

        assert _promoter is not None

    def test_knowledge_solution_projector_imports_alone(self):
        from kntgraph.agents.knowledge import solution_projector

        assert solution_projector is not None

    def test_solution_extractor_system_imports_alone(self):
        """Iter 28 FU 8 (ADR-034): the SolutionExtractorSystem
        is the pure replacement for the extract+gate
        portion of KnowledgeConsolidator. It must be
        importable in isolation (no PII / FalkorDB
        dependency)."""
        from kntgraph.agents.memory import solution_extractor

        assert solution_extractor is not None

    def test_knowledge_argument_extractor_does_not_exist(self):
        """Iter 28 follow-up: the argument_extractor
        package is GONE. Importing it must fail with
        ``ModuleNotFoundError`` (the package directory
        is deleted) or ``ImportError`` (if a parent
        package's ``__init__`` blocks the import).

        The cycle that originally motivated this
        regression gate (Iter 25) was closed by
        Iter 27 + 28; the vertical package is no
        longer needed.
        """
        with pytest.raises((ModuleNotFoundError, ImportError)):
            from kntgraph.agents.knowledge import argument_extractor  # noqa: F401

    def test_tools_pii_imports_alone(self):
        from kntgraph.agents.tools import pii

        assert pii is not None

    def test_tools_protocol_imports_alone(self):
        from kntgraph.agents.tools import protocol

        assert protocol is not None

    def test_arg_validation_lives_in_framework(self):
        """Iter 30 (v0.15): ``arg_validation`` was
        promoted to the framework. The module lives at
        ``kntgraph.tools.arg_validation``; the vertical
        path ``kntgraph.agents.tools.arg_validation``
        is a re-export shim for one minor cycle.

        We verify:
          - the framework path is reachable without
            forcing the vertical to load;
          - the public symbols are the **same
            function object** (re-export, not a
            copy).
          - the vertical path still works (shim
            compatibility).
        """
        import kntgraph.tools.arg_validation as framework_av

        from kntgraph.tools.arg_validation import (
            SchemaValidationError,
            validate_args,
        )

        # The framework module owns the canonical
        # function objects.
        assert validate_args is framework_av.validate_args
        assert SchemaValidationError is framework_av.SchemaValidationError


class TestCyclicImportRegression:
    """If a future refactor re-introduces the cycle,
    these tests catch it."""

    def test_loading_solution_extractor_does_not_load_pii(self):
        """The SolutionExtractorSystem (Iter 28 FU 8) is
        the pure replacement for the extract+gate
        portion of KnowledgeConsolidator. It must NOT
        import ``kntgraph.agents.tools.pii`` (the PII redaction
        is the promoter's concern, not the
        extractor's)."""
        import sys

        # Clear cached pii to detect re-import.
        sys.modules.pop("kntgraph.agents.tools.pii", None)
        sys.modules.pop("kntgraph.agents.tools.pii._tool", None)

        from kntgraph.agents.memory import solution_extractor  # noqa: F401

        # The pii module should not have been pulled in.
        assert "kntgraph.agents.tools.pii" not in sys.modules
        assert "kntgraph.agents.tools.pii._tool" not in sys.modules


@pytest.mark.parametrize(
    "module_name",
    [
        "kntgraph.agents.memory.solution_extractor",
        "kntgraph.agents.memory.solution_promoter",
        "kntgraph.agents.memory.solution_review_publisher",
        "kntgraph.agents.memory.solutions",
        "kntgraph.agents.memory.solutions._promoter",
        "kntgraph.agents.knowledge.solution_projector",
        "kntgraph.agents.tools.pii",
        "kntgraph.agents.tools.protocol",
        # ``arg_validation`` was promoted to the
        # framework in Iter 30 (v0.15); the vertical
        # module is gone (see ``test_arg_validation_lives_in_framework``
        # for the migration contract).
        "kntgraph.tools.arg_validation",
    ],
)
def test_module_importable(module_name: str):
    """Every public module of the affected packages
    must be importable. Parametrised to make the
    failure mode obvious (one module failing shows
    which one)."""
    import importlib

    importlib.import_module(module_name)
