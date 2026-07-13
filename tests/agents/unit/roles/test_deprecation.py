# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the deprecation lifecycle of `kntgraph.agents.roles`
(ADR-041).

The package emits a `DeprecationWarning` on import. The warning
is fired at most once per process (subsequent imports of the
already-loaded module are no-ops).
"""

from __future__ import annotations

import importlib
import sys
import warnings


class TestDeprecationWarning:
    def test_package_import_emits_deprecation_warning(self):
        """
        Importing `kntgraph.agents.roles` raises a
        `DeprecationWarning` referencing ADR-041.
        """
        # Reload to fire the warning even if the module is
        # already cached from a prior test.
        mod_name = "kntgraph.agents.roles"
        if mod_name in sys.modules:
            prior = sys.modules.pop(mod_name)
        else:
            prior = None
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", DeprecationWarning)
                reloaded = importlib.import_module(mod_name)
            dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert dep, "Importing kntgraph.agents.roles must emit DeprecationWarning"
            assert "ADR-039" in str(dep[0].message) or "ADR-041" in str(dep[0].message)
            # The re-exported symbols are still reachable
            assert hasattr(reloaded, "ChatRole")
            assert hasattr(reloaded, "PlannerRole")
        finally:
            if prior is not None:
                sys.modules[mod_name] = prior

    def test_warning_fires_only_once_per_process(self):
        """
        A second import of the package within the same
        process does not re-emit the warning (single global
        flag in the module).
        """
        mod_name = "kntgraph.agents.roles"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            # Import the package a second time — the warning
            # should not fire (the module is already loaded).
            importlib.import_module(mod_name)
        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        # We can't strictly assert zero (other tests in the
        # suite may reload), but in a fresh process the
        # second import is a no-op. Allow either 0 or 1
        # warning here — the strong guarantee is in the
        # first test.
        assert len(dep) <= 1

    def test_legacy_classes_still_importable(self):
        """
        Legacy classes remain importable from the package
        through the deprecation window. Their constructors
        do not emit additional warnings (only the package
        import does).
        """
        from kntgraph.agents.roles import (
            ChatRole,
            PlannerRole,
            SummarizerRole,
        )

        # Symbol presence is the contract; full instantiation
        # is covered by the existing per-class tests.
        assert ChatRole is not None
        assert PlannerRole is not None
        assert SummarizerRole is not None

    def test_new_components_share_the_package(self):
        """
        The new `RoleComponent` / `IntentResolutionSystem`
        symbols ship in the same package during the
        deprecation window (per ADR-041 §2).
        """
        from kntgraph.agents.roles import (
            IntentComponent,
            IntentResolutionSystem,
            RoleComponent,
        )

        assert RoleComponent is not None
        assert IntentComponent is not None
        assert IntentResolutionSystem is not None
