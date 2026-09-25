# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Redis sub-config (mixin).

Holds the connection URL, pool sizing, the in-process
fakeredis toggle used by benchmarks / CI smoke tests,
and the namespace prefix that scopes every Redis key
(ADR-076).

The mixin does NOT set ``env_prefix``; the parent
``Settings`` pins ``"KNT_"`` and env vars like
``KNT_REDIS_URL`` map to ``redis_url``.

Namespace prefix
----------------

``redis_key_prefix`` (env: ``KNT_REDIS_KEY_PREFIX``)
defaults to ``""`` -- the pre-ADR-076 wire format,
byte-for-byte. Operators that host two services on
the same Redis set it to e.g. ``acme-billing:`` and
the framework prepends it to every key it writes or
reads (``knt:agents:<id>:events`` becomes
``acme-billing:knt:agents:<id>:events``).

The prefix is **not** a security boundary (Redis has
no per-prefix ACL); it is namespace scoping so two
services do not cross-talk. See ADR-076 §1.4 / §3.2.

Validation is enforced once at construction by
:func:`field_validator` so a typo fails fast at boot,
not silently on the first write.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from kntgraph.infra.config._base import BaseSettings
from kntgraph.infra.redis._prefix import validate_prefix


class RedisSettingsMixin(BaseSettings):
    """Connection pool, URL, fakeredis toggle, key prefix."""

    redis_url: str = Field(default="redis://localhost:6379")
    redis_max_connections: int = Field(default=50)
    # In-process fakeredis toggle for benchmarks / CI smoke
    # tests; never set in production.
    redis_fake: bool = Field(default=False)
    # Namespace prefix for every Redis key the framework
    # writes. Empty string preserves the pre-ADR-076
    # behaviour. See ``infra.redis._prefix`` for the
    # composition rules and the validation pattern.
    redis_key_prefix: str = Field(default="")

    @field_validator("redis_key_prefix")
    @classmethod
    def _check_key_prefix(cls, value: str) -> str:
        """Fail-fast on a malformed prefix at boot.

        Pydantic calls this once during ``Settings``
        construction; the per-key-build path does not
        re-validate (the prefix cannot change after
        boot, so checking twice would be wasted work).
        """
        validate_prefix(value)
        return value
