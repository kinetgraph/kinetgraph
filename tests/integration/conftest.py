# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration test configuration.

Configures Redis for integration tests. FalkorDB integration was
removed in F4; tests for it will be re-added in a future F8
(GraphRAG) phase.
"""

import pytest
import pytest_asyncio

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import redis.asyncio as aioredis


@pytest_asyncio.fixture(scope="function")
async def redis_client():
    """
    Real Redis client for integration tests.

    Uses database 15 to avoid conflicts with production data.
    Database is flushed before and after tests.
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
        print("✓ Connected to Redis")
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")
        return

    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def clean_redis(redis_client):
    """Flush Redis before each test."""
    await redis_client.flushdb()
    yield redis_client
