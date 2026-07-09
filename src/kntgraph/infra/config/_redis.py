# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis sub-config (mixin).

Holds the connection URL, pool sizing, and the
in-process fakeredis toggle used by benchmarks / CI
smoke tests.

The mixin does NOT set ``env_prefix``; the parent
``Settings`` pins ``"KNT_"`` and env vars like
``KNT_REDIS_URL`` map to ``redis_url``.
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class RedisSettingsMixin(BaseSettings):
    """Connection pool, URL, fakeredis toggle."""

    redis_url: str = Field(default="redis://localhost:6379")
    redis_max_connections: int = Field(default=50)
    # In-process fakeredis toggle for benchmarks / CI smoke
    # tests; never set in production.
    redis_fake: bool = Field(default=False)
