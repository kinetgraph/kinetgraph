# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
make_redis_client — choose between real Redis and `fakeredis`.

Opt-in env: `FMH_REDIS_FAKE=1` switches to an in-process
`fakeredis.aioredis.FakeRedis` so examples run without a
Redis container. The default (`0` or unset) uses real Redis
at `localhost:6379` — unchanged from the previous examples.

The helper is intentionally tiny: it does NOT auto-detect
whether a Redis server is reachable, and does NOT fall back
to `fakeredis` on connection failure. Explicit env wins.

`fakeredis>=2.20` is declared as a `[dev]` extra in
`kntgraph`. See `examples/README.md` for the rationale.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis.asyncio as aioredis


def make_redis_client() -> "aioredis.Redis":
    """
    Build an async Redis client.

    - `FMH_REDIS_FAKE=1` → `fakeredis.aioredis.FakeRedis(decode_responses=False)`
      (in-process, no Docker required; surfaces the same
      `xadd` / `xrange` / `xrevrange` / `EVALSHA` calls used
      by `EventLog`).
    - otherwise → `redis.asyncio.from_url("redis://localhost:6379")`.

    `decode_responses=False` matches the `EventLog` wire format
    (bytes in, bytes out).
    """
    if os.environ.get("FMH_REDIS_FAKE") == "1":
        try:
            import fakeredis.aioredis as fakeredis_aioredis
        except ImportError as e:
            raise ImportError(
                "FMH_REDIS_FAKE=1 was set but `fakeredis` is not "
                "installed. Run `uv sync --extra dev` for "
                "kntgraph."
            ) from e
        return fakeredis_aioredis.FakeRedis(decode_responses=False)
    import redis.asyncio as aioredis

    return aioredis.from_url("redis://localhost:6379")


__all__ = ["make_redis_client"]
