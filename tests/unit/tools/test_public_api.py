# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression test for the ``kntgraph.tools`` public
surface (ADR-036 §2.4 "Developer Experience").

The four new modules (``worker``, ``manager``, ``router``,
``system``) must be re-exported from
``kntgraph.tools.__init__`` so consumers can write::

    from kntgraph.tools import tool_worker, WorkerManager, ToolRouter, ToolAwareSystem

without reaching into the sub-modules. This test is
the deletion gate: if a future refactor un-exports one
of these names, the test fails.
"""

from __future__ import annotations

import kntgraph.tools as tools_pkg


def test_tool_worker_is_exported() -> None:
    """``@tool_worker`` decorator must be importable
    from the framework's tools package root.
    """
    from kntgraph.tools import tool_worker

    assert callable(tool_worker)
    assert tool_worker is tools_pkg.tool_worker


def test_worker_manager_is_exported() -> None:
    """``WorkerManager`` must be importable from the
    framework's tools package root.
    """
    from kntgraph.tools import WorkerManager

    assert isinstance(WorkerManager, type)
    assert WorkerManager is tools_pkg.WorkerManager


def test_tool_router_is_exported() -> None:
    """``ToolRouter`` must be importable from the
    framework's tools package root.
    """
    from kntgraph.tools import ToolRouter

    assert isinstance(ToolRouter, type)
    assert ToolRouter is tools_pkg.ToolRouter


def test_tool_aware_system_is_exported() -> None:
    """``ToolAwareSystem`` mixin must be importable from
    the framework's tools package root.
    """
    from kntgraph.tools import ToolAwareSystem

    assert isinstance(ToolAwareSystem, type)
    assert ToolAwareSystem is tools_pkg.ToolAwareSystem


def test_all_advertised_names_in_dunder_all() -> None:
    """Every name listed in the package's ``__all__``
    must be importable. This is the public contract
    for the framework's tools module.
    """
    for name in tools_pkg.__all__:
        assert hasattr(tools_pkg, name), (
            f"kntgraph.tools advertises {name!r} in __all__ "
            f"but the module does not export it"
        )


def test_no_cyclic_import_on_tools_package() -> None:
    """Importing ``kntgraph.tools`` (the package
    root) must not trigger a circular import. The
    four new modules each import from
    ``kntgraph.core`` and ``kntgraph.stream``;
    the package ``__init__`` must compose them
    without forming a cycle back into ``tools.*``.
    """
    # The smoke import at the top of the file already
    # passed; this test is here to make the regression
    # explicit and to fail with a clear message if
    # someone breaks the contract.
    assert tools_pkg.__name__ == "kntgraph.tools"
    # All four new exports are reachable from the
    # package root in a single import.
    expected = {
        "tool_worker",
        "WorkerManager",
        "ToolRouter",
        "ToolAwareSystem",
    }
    assert expected.issubset(set(tools_pkg.__all__))
