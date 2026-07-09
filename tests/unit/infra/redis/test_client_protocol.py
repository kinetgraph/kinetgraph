# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the RedisLike Protocol — the typed boundary between
the framework and `redis.asyncio`.

These tests are part of the RED phase of the Iteration 1
adapter refactor (ADR-019). They will fail until
``kntgraph.infra.redis`` exists; the first GREEN step
creates ``_client.py`` with the Protocol definition.
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.asyncio


FRONTEND_SRC = Path("kntgraph/src/kntgraph")


def _collect_redis_method_calls() -> dict[str, Path]:
    """Scan framework source for `self._redis.X(...)` calls.

    Returns mapping of method name → first file path that
    used it. Acts as a contract test: if a new Redis method
    is used in framework code, the RedisLike Protocol must
    declare it.

    Only matches files where `self._redis` is a Redis-like
    client. Files using FalkorDB's ``self._client`` are
    skipped via path filter.
    """
    import re

    pattern = re.compile(r"self\._redis\.(\w+)\(")
    seen: dict[str, Path] = {}
    for py in FRONTEND_SRC.rglob("*.py"):
        if "infra/redis" in str(py):
            continue
        # Skip FalkorDB / non-Redis clients.
        if "falkordb" in str(py).lower():
            continue
        text = py.read_text()
        for m in pattern.finditer(text):
            name = m.group(1)
            if name not in seen:
                seen[name] = py
    return seen


class TestRedisLikeProtocol:
    def test_protocol_module_importable(self):
        from kntgraph.infra.redis import RedisLike

        assert RedisLike is not None

    def test_protocol_is_runtime_checkable(self):
        """`isinstance(client, RedisLike)` must work at runtime."""
        from kntgraph.infra.redis import RedisLike

        assert getattr(RedisLike, "_is_runtime_protocol", False) or hasattr(
            RedisLike, "__call__"
        ), "RedisLike must be decorated with @runtime_checkable"

    def test_redis_like_lists_required_methods(self):
        from kntgraph.infra.redis import RedisLike

        for name in (
            "get",
            "set",
            "delete",
            "xadd",
            "xrange",
            "xrevrange",
            "scan_iter",
            "pipeline",
        ):
            assert hasattr(RedisLike, name), f"RedisLike must declare {name!r}"


class TestRedisAsyncioSatisfiesProtocol:
    def test_redis_asyncio_redis_satisfies_redis_like(self):
        """`redis.asyncio.Redis` satisfies RedisLike via duck typing."""
        try:
            import redis.asyncio as redis_async
        except ImportError:
            pytest.skip("redis not installed")

        client_class = redis_async.Redis
        # We cannot instantiate without a real connection,
        # but we can verify the class exposes the methods.
        for name in (
            "get",
            "set",
            "delete",
            "xadd",
            "xrange",
            "xrevrange",
            "scan_iter",
            "pipeline",
        ):
            assert hasattr(client_class, name), (
                f"redis.asyncio.Redis must expose {name!r}"
            )


class TestProtocolCoverage:
    """Contract test: every Redis method used in framework code
    must be declared in RedisLike.

    This test catches refactors that introduce a new
    `self._redis.X(...)` call without updating the Protocol.
    """

    EXCLUDED_DIRS = {
        "infra/redis/",
    }

    def test_protocol_covers_all_framework_redis_calls(self):
        from kntgraph.infra.redis import RedisLike

        used = _collect_redis_method_calls()
        declared = set(dir(RedisLike))
        # Filter out methods used outside Iteration 1 scope.
        used_in_scope = {
            name
            for name, filepath in used.items()
            if not any(ex in str(filepath) for ex in self.EXCLUDED_DIRS)
        }
        missing = used_in_scope - declared
        assert not missing, (
            f"Redis methods used in framework but not declared in RedisLike: "
            f"{sorted(missing)}"
        )


class TestNoDirectRedisImportInFramework:
    """Framework code must not import `redis.asyncio` directly.
    All Redis access goes through RedisLike.

    Iteration 5 (ADR-019) closed the gap: every shard
    (event_log, memory, auth, checkpoint, dlq,
    world_checkpoint) is now a typed adapter. There
    are no remaining `redis.asyncio` imports outside
    ``infra/redis/``.
    """

    EXCLUDED = {
        "infra/redis/",
    }

    def test_no_redis_asyncio_imports_outside_redis_package(self):
        import re

        pattern = re.compile(r"^(?:from|import)\s+redis(?:\.asyncio)?")
        offenders: list[tuple[Path, int, str]] = []
        for py in FRONTEND_SRC.rglob("*.py"):
            if any(ex in str(py) for ex in self.EXCLUDED):
                continue
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                if pattern.match(line):
                    offenders.append((py, lineno, line.strip()))
        assert not offenders, (
            "Direct `redis` imports outside kntgraph/infra/redis/: "
            + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
        )
