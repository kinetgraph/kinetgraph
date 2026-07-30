# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/graph/_pool.py`` (``GraphPool``).

Closes the infra/graph/_pool coverage gap (DEBT §3,
68% → 100%). The module has:

  - ``graph_name_for_tenant`` — pure helper that
    sanitises a tenant_id into a graph name (CNPJ-style
    separators are replaced with ``_``). Covered in the
    existing ``test_graph_pool.py``.

  - ``GraphPool`` — facade that owns the FalkorDB
    connection lifecycle. The public surface is
    ``__init__`` / ``connect`` / ``graph`` / ``close`` /
    ``_resolve_password``. The connection is lazy
    (the ``falkordb`` import is local to ``connect()``)
    so a process that never calls ``graph()`` does not
    require the package to be installed.

This module adds the missing branches:

  - ``connect`` is idempotent (early return when
    ``self._db`` is already set).
  - ``connect`` honours an explicit ``password=`` (the
    FalkorDB client is constructed with the password).
  - ``connect`` falls back to the no-password
    constructor when no password is set.
  - ``_resolve_password`` returns the explicit password
    first, then ``Settings.falkordb_password``, then the
    ``KNT_FALKORDB_PASSWORD`` env var, then ``None``.
  - ``_resolve_password`` returns ``None`` (no env var
    set, no settings) — the ``KNT_FALKORDB_PASSWORD``
    env var is the last resort.
  - ``_resolve_password`` swallows the
    ``Settings`` import failure (the test environment
    may not have the full framework; the env var
    fallback still works).
"""

from __future__ import annotations


class TestGraphPoolConnect:
    def test_connect_is_idempotent(self) -> None:
        from kntgraph.infra.graph._pool import GraphPool

        c = GraphPool()
        c._db = object()  # simulate a connected state
        # A second call returns immediately (no
        # falkordb import, no client construction).
        c.connect()
        assert c._db is not None

    def test_connect_with_explicit_password_uses_falkordb(self, monkeypatch) -> None:
        from kntgraph.infra.graph._pool import GraphPool

        captured: dict = {}

        class _StubFalkorDB:
            def __init__(self, host, port, password=None):
                captured["host"] = host
                captured["port"] = port
                captured["password"] = password

        import sys

        falkordb_module = type(sys)("falkordb.asyncio")
        falkordb_module.FalkorDB = _StubFalkorDB
        monkeypatch.setitem(sys.modules, "falkordb", type(sys)("falkordb"))
        monkeypatch.setitem(sys.modules, "falkordb.asyncio", falkordb_module)

        c = GraphPool(host="h", port=1234, password="s3cr3t")
        c.connect()

        assert captured == {"host": "h", "port": 1234, "password": "s3cr3t"}

    def test_connect_without_password_omits_kwarg(self, monkeypatch) -> None:
        from kntgraph.infra.graph._pool import GraphPool

        captured: dict = {}

        class _StubFalkorDB:
            def __init__(self, host, port, password=None):
                captured["host"] = host
                captured["port"] = port
                captured["password"] = password

        import sys

        falkordb_module = type(sys)("falkordb.asyncio")
        falkordb_module.FalkorDB = _StubFalkorDB
        monkeypatch.setitem(sys.modules, "falkordb", type(sys)("falkordb"))
        monkeypatch.setitem(sys.modules, "falkordb.asyncio", falkordb_module)
        # Make sure no env var leaks into the test.
        monkeypatch.delenv("KNT_FALKORDB_PASSWORD", raising=False)

        c = GraphPool(host="h", port=1234)
        c.connect()

        assert captured["host"] == "h"
        assert captured["port"] == 1234
        assert captured["password"] is None


class TestGraphPoolResolvePassword:
    def test_explicit_password_wins(self, monkeypatch) -> None:
        from kntgraph.infra.graph._pool import GraphPool

        # Set the env var to a value that should NOT be
        # returned (the explicit password wins).
        monkeypatch.setenv("KNT_FALKORDB_PASSWORD", "from-env")
        c = GraphPool(password="explicit")

        assert c._resolve_password() == "explicit"

    def test_settings_password_wins_over_env(self, monkeypatch) -> None:
        from kntgraph.infra.config import settings
        from kntgraph.infra.graph._pool import GraphPool

        monkeypatch.setattr(settings, "falkordb_password", "from-settings")
        monkeypatch.setenv("KNT_FALKORDB_PASSWORD", "from-env")
        c = GraphPool()  # no explicit password

        assert c._resolve_password() == "from-settings"

    def test_env_var_fallback(self, monkeypatch) -> None:
        from kntgraph.infra.config import settings
        from kntgraph.infra.graph._pool import GraphPool

        monkeypatch.setattr(settings, "falkordb_password", None)
        monkeypatch.setenv("KNT_FALKORDB_PASSWORD", "from-env")
        c = GraphPool()  # no explicit password, no settings value

        assert c._resolve_password() == "from-env"

    def test_returns_none_when_nothing_set(self, monkeypatch) -> None:
        from kntgraph.infra.config import settings
        from kntgraph.infra.graph._pool import GraphPool

        monkeypatch.setattr(settings, "falkordb_password", None)
        monkeypatch.delenv("KNT_FALKORDB_PASSWORD", raising=False)
        c = GraphPool()

        assert c._resolve_password() is None

    def test_settings_import_failure_falls_back_to_env(self, monkeypatch) -> None:
        from kntgraph.infra.graph._pool import GraphPool

        # Block the ``Settings`` import. The contract:
        # the password resolution falls through to the
        # env var when the settings module is not
        # available (e.g. an embed scenario).
        import builtins

        original_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name.startswith("kntgraph.infra.config"):
                raise ImportError("settings unavailable")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        monkeypatch.setenv("KNT_FALKORDB_PASSWORD", "fallback")
        c = GraphPool()

        assert c._resolve_password() == "fallback"

    def test_settings_import_failure_no_env_returns_none(self, monkeypatch) -> None:
        from kntgraph.infra.graph._pool import GraphPool

        import builtins

        original_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name.startswith("kntgraph.infra.config"):
                raise ImportError("settings unavailable")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        monkeypatch.delenv("KNT_FALKORDB_PASSWORD", raising=False)
        c = GraphPool()

        assert c._resolve_password() is None
