# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration test configuration for kntgraph.agents.

Reuses the same Redis fixture as kntgraph (db 15, flushdb
before/after).
"""

import pytest
import pytest_asyncio

import redis.asyncio as aioredis


@pytest_asyncio.fixture(scope="function")
async def redis_client():
    client = aioredis.Redis(host="localhost", port=6379, db=15, decode_responses=False)
    try:
        await client.ping()
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")
        return
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def clean_redis(redis_client):
    await redis_client.flushdb()
    yield redis_client
