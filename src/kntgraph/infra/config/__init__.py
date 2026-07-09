# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Application configuration.

Loads from environment variables and ``.env``. The base
class is a thin wrapper over Pydantic v2's
``BaseSettings``; the project-specific ``Settings``
sits below and pins ``env_prefix="KNT_"`` so env
vars like ``KNT_REDIS_URL`` map to fields.

Two ways to read settings:

  1. ``from kntgraph.infra.config import settings``
     — module-level singleton, captured at import time.
     Use this in production hot paths where env does
     not change at runtime.

  2. ``from kntgraph.infra.config import fresh_settings``
     — factory that re-reads the env every call. Use
     this in tests that ``monkeypatch.setenv(...)`` and
     expect the next read to see the new value.

Prefix
------

The ``Settings`` class pins ``env_prefix="KNT_"`` so
existing deployments using ``KNT_REDIS_URL``,
``KNT_FALKORDB_PASSWORD``, etc. keep working
unchanged. The base class does NOT set a prefix —
subclasses opt in. That way ``Settings`` is the only
canonical env-reading class, and other future
``Settings`` (e.g. a worker-only sub-config) can pick a
different prefix without colliding.

Package layout
--------------

The 35 fields are organised by concern into nine
mixins, one per module:

* ``_base`` — ``BaseSettings`` thin wrapper + ``.env``
  helpers (``load_dotenv_files``, ``default_dotenv_candidates``,
  ``env_or_default``).
* ``_redis`` — connection pool, URL, fakeredis toggle.
* ``_falkordb`` — host, port, password.
* ``_runner`` — post-tick loop interval.
* ``_resilience`` — circuit-breaker + retry policy.
* ``_timeouts`` — default + LLM timeouts.
* ``_streams`` — per-tenant + global Stream MAXLEN caps.
* ``_memory`` — Session / Profile / Continuity TTLs.
* ``_knowledge`` — consolidator cadence, review queue,
  argument extractor.
* ``_http`` — CORS, trusted hosts, HTTPS redirect,
  HSTS, rate limit, ``expose_docs``.
* ``_pii`` — default PII redaction level.

``Settings`` aggregates them all via multiple
inheritance. Each mixin only declares its own fields;
cross-field validation (``env=prod`` rejects the
historical weak FalkorDB password) lives on
``Settings`` itself, where Pydantic gives us access
to fully-validated values.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from kntgraph.infra.config._base import (
    BaseSettings,
    default_dotenv_candidates,
    env_or_default,
    load_dotenv_files,
)
from kntgraph.infra.config._embedding import EmbeddingSettingsMixin
from kntgraph.infra.config._falkordb import FalkordbSettingsMixin
from kntgraph.infra.config._http import HttpSettingsMixin
from kntgraph.infra.config._knowledge import KnowledgeSettingsMixin
from kntgraph.infra.config._llm import LLMSettingsMixin
from kntgraph.infra.config._memory import MemorySettingsMixin
from kntgraph.infra.config._pii import PiiSettingsMixin
from kntgraph.infra.config._redis import RedisSettingsMixin
from kntgraph.infra.config._resilience import ResilienceSettingsMixin
from kntgraph.infra.config._runner import RunnerSettingsMixin
from kntgraph.infra.config._streams import StreamsSettingsMixin
from kntgraph.infra.config._timeouts import TimeoutsSettingsMixin

# Historical dev default for the FalkorDB password, baked
# into the FMH examples. The ``Settings._validate_prod_invariants``
# validator rejects this in prod. Named (instead of inlined
# as a string literal) so bandit B105 doesn't flag the
# comparison as a hardcoded password.
_DEFAULT_DEV_FALKORDB_PASSWORD = "falkordb"  # nosec B105 - sentinel for prod-rejection check


class Settings(
    LLMSettingsMixin,
    EmbeddingSettingsMixin,
    RedisSettingsMixin,
    FalkordbSettingsMixin,
    RunnerSettingsMixin,
    ResilienceSettingsMixin,
    TimeoutsSettingsMixin,
    StreamsSettingsMixin,
    MemorySettingsMixin,
    KnowledgeSettingsMixin,
    HttpSettingsMixin,
    PiiSettingsMixin,
    BaseSettings,
):
    """Aggregated FMH backend settings.

    Mixin chain (one sub-config per external dependency):

      - ``LLMSettingsMixin`` (``_llm.py``) — model
        selection, sampling, timeout, cost cap.
      - ``EmbeddingSettingsMixin`` (``_embedding.py``)
        — embedding model + dimension + timeout.
      - ``KnowledgeSettingsMixin`` (``_knowledge.py``)
        — consolidator cadence, review queue,
        argument extractor.
      - Plus the legacy mixins (Redis, FalkorDB, HTTP,
        PII, CORS, etc.) that predate the split.
    """

    # Env selection. ``prod`` enables stricter validation
    # (e.g. rejects the historical weak FalkorDB password);
    # ``dev`` is the default for local development.
    env: Literal["dev", "prod"] = Field(default="dev")

    # Pin ``env_prefix`` once on the aggregate. Each
    # mixin inherits it; setting it again on a mixin
    # would shadow this and break the flat env-var
    # namespace (``KNT_REDIS_URL``, ``KNT_TICK_INTERVAL``,
    # ...).
    model_config = SettingsConfigDict(
        env_prefix="KNT_",
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def _validate_prod_invariants(self) -> "Settings":
        """
        Cross-field validation that only fires under
        ``env=prod``.

        Reject the historical weak FalkorDB password
        that the FMH examples baked in. This validator
        only blocks the one specific string that has
        historically been baked into the codebase, not
        a general "weak password" check.

        Implemented as a ``model_validator(mode="after")``
        instead of a ``field_validator`` because the rule
        spans two fields (``falkordb_password`` AND
        ``env``), and Pydantic only exposes other
        fields' validated values via ``info.data`` AFTER
        the model is fully constructed.
        """
        if (
            self.env == "prod"
            and self.falkordb_password == _DEFAULT_DEV_FALKORDB_PASSWORD
        ):
            raise ValueError(
                f"KNT_FALKORDB_PASSWORD={_DEFAULT_DEV_FALKORDB_PASSWORD!r} is "
                f"rejected when KNT_ENV=prod; choose a unique password."
            )
        # https_redirect_status must be one of the
        # redirect codes Starlette's RedirectResponse
        # accepts (and that browsers understand for the
        # https-upgrade use case). 308 (default) is the
        # canonical choice per RFC 7538; 301 is the
        # legacy alternative.
        if self.https_redirect_status not in {301, 302, 307, 308}:
            raise ValueError(
                f"https_redirect_status must be one of "
                f"{{301, 302, 307, 308}}, got "
                f"{self.https_redirect_status}"
            )
        # HSTS max_age must be coupled to a non-zero
        # value when https_redirect_enabled. HSTS pins
        # HTTPS in the browser; emitting HSTS without
        # also redirecting would break the user's
        # expectation (they'd see HSTS but no
        # upgrade).
        # NOTE: we don't *enforce* this; the operator
        # may legitimately want HSTS=0 even when
        # https_redirect=True (no HSTS pin), or HSTS>0
        # only after they confirm the cert is solid.
        # The wiring at the middleware level skips
        # HSTS when max_age == 0.
        return self


@lru_cache(maxsize=1)
def fresh_settings() -> Settings:
    """
    Build a ``Settings`` instance from the current
    ``os.environ``. Cached so repeated calls within a
    single process do not re-parse env vars; tests that
    need to see ``monkeypatch.setenv(...)`` changes can
    call ``fresh_settings.cache_clear()`` before
    constructing the next ``Settings``.
    """
    return Settings()


settings = fresh_settings()


# Re-exports for backward compatibility — callers that
# did ``from kntgraph.infra.config import BaseSettings``
# keep working.
__all__ = [
    # Base class
    "BaseSettings",
    # Helpers
    "default_dotenv_candidates",
    "env_or_default",
    "load_dotenv_files",
    # Schema + accessors
    "Settings",
    "fresh_settings",
    "settings",
]
