# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``kntgraph.infra.config`` — the
shared env loader / settings base class.

These tests were originally in
``fmh_core/tests/test_config.py`` (the standalone
``fmh_core`` package). When the package was merged
into ``kntgraph``, the tests moved here. The class
``BaseSettings`` (formerly ``FMHBaseSettings``) is now
defined in ``kntgraph.infra.config`` alongside the
project-specific ``Settings`` (which pins
``env_prefix="KNT_"``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kntgraph.infra.config import (
    BaseSettings,
    default_dotenv_candidates,
    env_or_default,
    load_dotenv_files,
)


class _Sample(BaseSettings):
    """A throwaway settings class used by the tests.

    Note: ``BaseSettings`` has no prefix by default —
    env vars are read as-is (e.g. ``FOO`` maps to
    ``foo``). The ``Settings`` class in this same module
    pins ``env_prefix="KNT_"``; this sample is the
    prefix-less form so the tests read the vars without
    the prefix.
    """

    foo: str = "default"
    bar: int = 0


class TestBaseSettings:
    def test_default_when_no_env(self, monkeypatch) -> None:
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAR", raising=False)
        s = _Sample()
        assert s.foo == "default"
        assert s.bar == 0

    def test_env_overrides_default(self, monkeypatch) -> None:
        monkeypatch.setenv("FOO", "from-env")
        monkeypatch.setenv("BAR", "42")
        s = _Sample()
        assert s.foo == "from-env"
        assert s.bar == 42

    def test_unknown_env_var_ignored(self, monkeypatch) -> None:
        # `extra="ignore"` means a stray env var that
        # does not match any field must not crash
        # construction — important because the same
        # process loads several `Settings` (backend,
        # LLM, router) with different prefixes.
        monkeypatch.setenv("FOO", "x")
        monkeypatch.setenv("NOT_A_FIELD", "y")
        s = _Sample()
        assert s.foo == "x"

    def test_int_coerced_from_string(self, monkeypatch) -> None:
        monkeypatch.setenv("BAR", "not-an-int")
        with pytest.raises(Exception):
            _Sample()


class TestEnvOrDefault:
    def test_unset_returns_default(self, monkeypatch) -> None:
        monkeypatch.delenv("KNT_NONEXISTENT", raising=False)
        assert env_or_default("KNT_NONEXISTENT", "fallback") == "fallback"

    def test_set_returns_value(self, monkeypatch) -> None:
        monkeypatch.setenv("KNT_NONEXISTENT", "value")
        assert env_or_default("KNT_NONEXISTENT") == "value"

    def test_empty_returns_default(self, monkeypatch) -> None:
        # An empty env var is treated as unset so a
        # typo like `KNT_FOO=` does not bypass
        # downstream validation by injecting the empty
        # string.
        monkeypatch.setenv("KNT_NONEXISTENT", "   ")
        assert env_or_default("KNT_NONEXISTENT", "fallback") == "fallback"


class TestLoadDotenvFiles:
    def test_returns_empty_when_no_files(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.env"
        assert load_dotenv_files(missing) == []

    def test_loads_existing_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / "test.env"
        env_file.write_text("KNT_FOO=from-file\n")
        # Caller controls the override semantics; the
        # helper itself uses `override=False` so an
        # explicit env wins over the file.
        os.environ.pop("KNT_FOO", None)
        loaded = load_dotenv_files(env_file)
        assert loaded == [env_file]
        assert os.environ.get("KNT_FOO") == "from-file"
        os.environ.pop("KNT_FOO", None)

    def test_existing_env_wins_over_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / "test.env"
        env_file.write_text("KNT_FOO=from-file\n")
        os.environ["KNT_FOO"] = "from-env"
        load_dotenv_files(env_file)
        assert os.environ.get("KNT_FOO") == "from-env"
        os.environ.pop("KNT_FOO", None)


class TestDefaultDotenvCandidates:
    def test_returns_cwd_and_home(self) -> None:
        cands = default_dotenv_candidates()
        assert cands[0] == Path.cwd() / ".env"
        assert cands[1] == Path.home() / ".env"
