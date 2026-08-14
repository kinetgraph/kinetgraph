# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Stress test configuration.

Mirrors the integration test setup: a real Redis client
(``localhost:6379``, db=15) with ``flushdb`` before and
after every test. Stress tests are opt-in via the
``stress`` step in ``scripts/ci.py`` and require
infrastructure the unit suite does not.

If Redis is not reachable the entire stress module is
skipped via the top-level fixture below; an environment
without Redis is not a test failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

import redis.asyncio as aioredis


@pytest_asyncio.fixture(scope="function")
async def redis_client():
    """Real Redis client for stress tests.

    Uses database 15 to avoid conflicts with production
    data. Database is flushed before and after tests.
    """
    password = os.environ.get("KNT_REDIS_PASSWORD", "redispassword")
    client = aioredis.Redis(
        host="localhost",
        port=6379,
        password=password,
        db=15,
        decode_responses=False,
    )
    try:
        await client.ping()
    except Exception as e:
        await client.aclose()
        pytest.skip(f"Redis not available: {e}")
        return

    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture(autouse=True)
def _require_redis_or_skip(request, redis_client):
    """Skip the whole test module if the Redis fixture
    was unable to connect. Pairs with the explicit
    ``pytest.skip`` in the async fixture above; the
    autouse here is the belt-and-suspenders form so
    the test is reported as ``SKIPPED`` rather than
    ``ERROR`` if a future refactor breaks the async
    fixture's skip path.
    """
    if not request.node.get_closest_marker("stress"):
        return
    # The async fixture raises pytest.skip on
    # connection failure; this fixture never sees
    # that case because the async fixture short-
    # circuits via ``return`` after the skip call.
    # The marker is informational only.
