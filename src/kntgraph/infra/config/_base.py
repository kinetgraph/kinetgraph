# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Application configuration — base utilities.

The canonical ``Settings`` (the FMH backend's
project-specific schema) lives in this package's
``__init__.py``, composed via multiple inheritance
from 12 mixin modules (one per external dependency:
LLM, embedding, Redis, FalkorDB, etc.). Each mixin
only declares its own fields; cross-field validation
(``env=prod`` rejects the historical weak FalkorDB
password) lives on the aggregated ``Settings`` class.

This module provides the **base utilities** shared
by every mixin and by the aggregated ``Settings``:

  - ``BaseSettings``: thin wrapper over Pydantic v2's
    ``BaseSettings``. Subclasses MUST set
    ``model_config`` to override ``env_prefix`` (the
    aggregate pins ``"FMH_"``).
  - ``load_dotenv_files``: walks a list of paths and
    loads the first existing ``.env``. Mirrors the
    legacy ``load_env`` behaviour (explicit env wins
    over file via ``override=False``).
  - ``default_dotenv_candidates``: conventional
    ``.env`` lookup paths (``<cwd>/.env`` then
    ``~/.env``).
  - ``env_or_default``: read ``os.environ[name]`` with
    empty-string treated as unset. Non-``Settings``
    callers (scripts, examples, helpers) use this for
    the same "env or fallback" rule.

Prefix model
------------

The aggregate's ``model_config`` pins
``env_prefix="FMH_"`` so existing deployments using
``FMH_REDIS_URL``, ``FMH_FALKORDB_PASSWORD``, etc.
keep working unchanged. The base class does NOT set
a prefix — subclasses opt in. That way the
aggregated ``Settings`` is the only canonical
env-reading class for the FMH backend, and other
future ``Settings`` (e.g. a worker-only sub-config)
can pick a different prefix without colliding.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings as _PydanticBaseSettings
from pydantic_settings import SettingsConfigDict


class BaseSettings(_PydanticBaseSettings):
    """
    Base class for every ``Settings`` in the FMH
    workspace.

    Subclasses MUST set ``model_config`` to override
    ``env_prefix``. The default ``extra="ignore"`` lets a
    single process load several ``Settings`` (backend,
    LLM, router) without one complaining about the
    other's variables.

    No prefix is set on this base — each subclass
    chooses its own. The aggregated ``Settings`` pins
    ``"FMH_"``; an LLM-only sub-config can pick
    ``"FMH_LLM_"`` etc.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


def load_dotenv_files(*paths: Path) -> list[Path]:
    """
    Load ``.env`` files in order; existing env vars win.

    Mirrors the legacy behaviour of
    ``kntgraph.agents.config.llm.load_env``: walks the given
    paths, returns the list of files that were loaded.
    The first file that exists wins; subsequent files are
    skipped. ``python-dotenv`` is used so the same
    ``override=False`` semantics apply (explicit env wins
    over the file).

    Returns an empty list if ``python-dotenv`` is not
    installed; the caller is expected to rely on real
    env vars in that case.
    """
    loaded: list[Path] = []
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return loaded
    for p in paths:
        if p.is_file():
            load_dotenv(p, override=False)
            loaded.append(p)
            break
    return loaded


def default_dotenv_candidates() -> list[Path]:
    """
    Conventional ``.env`` lookup paths.

    Mirrors the legacy ``load_env`` walk order:
      1. ``<cwd>/.env``
      2. ``~/.env``
    """
    return [Path.cwd() / ".env", Path.home() / ".env"]


def env_or_default(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Read ``name`` from ``os.environ``, returning
    ``default`` if unset or empty. Kept as a function
    (not a field) so non-``Settings`` callers —
    scripts, examples, helpers — can use the same "env
    or fallback" rule.

    Empty strings are treated as unset so a typo like
    ``FMH_FOO=`` does not accidentally set the value to
    the empty string and bypass validation downstream.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw


__all__ = [
    "BaseSettings",
    "default_dotenv_candidates",
    "env_or_default",
    "load_dotenv_files",
]
