# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression test: the framework import chain has
**no circular import**. The cycle
``kntgraph.agents.tools.cache`` → ``kntgraph.agents.tools.invoker``
→ ``kntgraph.stream.event_log`` → ``kntgraph.infra``
→ ``kntgraph.infra.graph._lite_pool`` →
``kntgraph.knowledge.graph`` →
``kntgraph.knowledge.falkordb.adapter`` →
``kntgraph.stream.event_log`` is structurally
eliminated.

This test is the cycle detection gate. If a future
refactor re-introduces the cycle (e.g. by moving
``EventLog`` back to eager import in
``knowledge.falkordb.adapter``), this test fails.

Background
----------

Iter 28 follow-up 2 (``LiteFalkorDBClient`` →
``LiteGraphPool``) inadvertently introduced this
cycle. The cycle was tolerable in production (tests
ran in isolation) but blocked any caller that did
``import kntgraph.agents.tools.cache`` directly. Iter 28
follow-up 4 (ADR-031) closed the previous
architectural debt (``GraphLike`` deletion) but did
not address this cycle. This iter (Iter 28 follow-up
5) closes it.

Root cause
~~~~~~~~~~

``knowledge.falkordb.adapter`` (FalkorDBProjector)
eagerly imported ``EventLog`` at module top level.
``EventLog`` is a concrete class in
``kntgraph.stream.event_log``. The chain
``kntgraph.agents.tools.cache`` → ``kntgraph.agents.tools.invoker``
→ ``kntgraph.stream.event_log`` (top of invoker.py)
→ ``...infra.redis._codec`` (via codec.py) →
``kntgraph.infra.__init__`` → ``...lite_graph_client``
→ ``kntgraph.knowledge`` → ``...falkordb.adapter``
→ ``...stream.event_log`` (CYCLE) was the trigger.

Fix
~~~

Move the ``EventLog`` import in
``knowledge.falkordb.adapter`` to ``TYPE_CHECKING``.
The class is only used as a type hint in the
constructor signature (``log: EventLog``). Runtime
usage is duck-typed (``self._log.list_agents()``,
``self._log.read()``); no name resolution needed at
runtime. AGENTS.md §1.5 explicitly permits this
pattern: "TYPE_CHECKING blocks podem importar types
concretos (sem custo de runtime)."

Other cycle candidates (``kntgraph.api.*``,
``kntgraph.memory.*``, ``kntgraph.runner.*``)
were audited and **do not exhibit this problem**
because they import ``EventLog`` after
``kntgraph.infra`` is fully initialised (the
``stream.event_log`` module finishes loading before
those modules are reached).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


class TestToolsCacheImportChain:
    """The ``kntgraph.agents.tools.cache`` import path is
    cycle-free."""

    def test_kntgraph_agents_tools_cache_imports_cleanly(self) -> None:
        """``import kntgraph.agents.tools.cache`` must NOT
        raise ``ImportError`` due to a cycle.

        Before this iter, the cycle was:
        ``kntgraph.agents.tools.cache`` →
        ``kntgraph.agents.tools.invoker`` →
        ``kntgraph.stream.event_log`` →
        ``kntgraph.infra`` →
        ``kntgraph.infra.graph._lite_pool`` →
        ``kntgraph.knowledge.graph`` →
        ``kntgraph.knowledge.falkordb.adapter`` →
        ``kntgraph.stream.event_log``.

        After this iter, the cycle is broken at
        ``knowledge.falkordb.adapter`` (moved
        ``EventLog`` import to ``TYPE_CHECKING``).
        """
        # Run a subprocess so any ``ImportError`` from
        # a cycle is reported cleanly without polluting
        # the test's own import state.
        import os
        from pathlib import Path

        script = textwrap.dedent(
            """
            import sys
            try:
                import kntgraph.agents.tools.cache  # noqa: F401
            except ImportError as e:
                # Detect cycle-induced ImportError.
                # Cycle message in CPython 3.12:
                # "cannot import name 'X' from partially
                # initialized module 'Y' (most likely due
                # to a circular import)"
                msg = str(e)
                if "circular import" in msg or "partially initialized" in msg:
                    print(f"CYCLE: {e}", file=sys.stderr)
                    sys.exit(2)
                raise
            print("OK")
            """
        )
        # Build PYTHONPATH: kntgraph/src and
        # kntgraph.agents/src (siblings of the workspace
        # root that contains this test's file).
        workspace_root = Path(__file__).parent.parent.parent.parent
        pythonpath = ":".join(
            [
                str(workspace_root / "kntgraph" / "src"),
                str(workspace_root / "kntgraph.agents" / "src"),
            ]
        )
        env = {**os.environ, "PYTHONPATH": pythonpath}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )
        # Exit code 2 = cycle (test failure).
        # Other non-zero = real import error (test failure).
        # Zero = success.
        assert result.returncode == 0, (
            f"kntgraph.agents.tools.cache import failed "
            f"(rc={result.returncode}):\n"
            f"stdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
        assert "OK" in result.stdout, f"Expected 'OK' in stdout, got: {result.stdout!r}"

    def test_eventlog_not_eagerly_imported_in_adapter(self) -> None:
        """``knowledge.falkordb.adapter`` must not
        import ``EventLog`` at module top level.

        The import must be under
        ``if TYPE_CHECKING:`` (or absent). This
        structurally prevents the cycle: if
        ``adapter.py`` is loaded during
        ``kntgraph.infra.__init__`` initialisation
        (before ``stream.event_log`` finishes
        loading), the cycle is broken because the
        import is never executed.
        """
        import ast
        from pathlib import Path

        adapter_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "kntgraph"
            / "knowledge"
            / "falkordb"
            / "adapter.py"
        )
        source = adapter_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Walk the module body looking for top-level
        # imports of EventLog from stream.event_log.
        # We want them to be inside a TYPE_CHECKING
        # block.
        top_level_eventlog_imports: list[ast.ImportFrom] = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and node.module.endswith("stream.event_log")
                    and any(alias.name == "EventLog" for alias in node.names)
                ):
                    top_level_eventlog_imports.append(node)

        # Now walk again looking for TYPE_CHECKING blocks.
        type_checking_imports: list[ast.ImportFrom] = []
        for node in tree.body:
            if isinstance(node, ast.If):
                test = node.test
                # ``if TYPE_CHECKING:`` compiles to
                # ``ast.Name(id='TYPE_CHECKING')``.
                if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                    for sub in node.body:
                        if (
                            isinstance(sub, ast.ImportFrom)
                            and sub.module
                            and sub.module.endswith("stream.event_log")
                            and any(alias.name == "EventLog" for alias in sub.names)
                        ):
                            type_checking_imports.append(sub)

        assert not top_level_eventlog_imports, (
            f"knowledge.falkordb.adapter must NOT import "
            f"EventLog at top level. Found: "
            f"{[ast.unparse(n) for n in top_level_eventlog_imports]}"
        )
        assert type_checking_imports, (
            "Expected EventLog import under "
            "TYPE_CHECKING block in "
            "knowledge.falkordb.adapter"
        )
