# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for resilience/fallback.py.

The fallback surface is intentionally tiny (3 functions) but
the contract has several non-obvious properties worth pinning
down: silent error swallowing, arg forwarding, chain
short-circuit, default-on-all-failure.
"""

from __future__ import annotations

import pytest

from kntgraph.resilience.fallback import (
    with_default_on_failure,
    with_fallback,
    with_fallback_chain,
)


pytestmark = pytest.mark.asyncio


class TestWithFallback:
    async def test_returns_primary_result_on_success(self):
        async def primary():
            return "primary"

        async def secondary():
            return "secondary"

        result = await with_fallback(primary, secondary, operation_name="t")
        assert result == "primary"

    async def test_falls_back_to_secondary_on_primary_exception(self):
        async def primary():
            raise RuntimeError("boom")

        async def secondary():
            return "secondary"

        result = await with_fallback(primary, secondary, operation_name="t")
        assert result == "secondary"

    async def test_forwards_positional_and_keyword_args(self):
        """
        The `operation_name` keyword is reserved by
        `with_fallback` and popped from kwargs before
        forwarding; it must NOT reach the underlying
        callable.
        """
        received: list[tuple] = []

        async def primary(*args, **kwargs):
            received.append(("primary", args, kwargs))
            raise RuntimeError("boom")

        async def secondary(*args, **kwargs):
            received.append(("secondary", args, kwargs))
            return "ok"

        result = await with_fallback(
            primary, secondary, 1, 2, key="value", operation_name="t"
        )
        assert result == "ok"
        # `operation_name` is consumed by with_fallback; only
        # `key` reaches the callables.
        assert received == [
            ("primary", (1, 2), {"key": "value"}),
            ("secondary", (1, 2), {"key": "value"}),
        ]

    async def test_reraises_if_secondary_also_fails(self):
        async def primary():
            raise RuntimeError("primary")

        async def secondary():
            raise RuntimeError("secondary")

        with pytest.raises(RuntimeError, match="secondary"):
            await with_fallback(primary, secondary, operation_name="t")


class TestWithDefaultOnFailure:
    async def test_returns_primary_result_on_success(self):
        async def primary():
            return "primary"

        result = await with_default_on_failure(primary, "default", operation_name="t")
        assert result == "primary"

    async def test_returns_default_on_primary_exception(self):
        async def primary():
            raise RuntimeError("boom")

        result = await with_default_on_failure(primary, "default", operation_name="t")
        assert result == "default"

    async def test_works_with_non_string_default(self):
        async def primary():
            raise RuntimeError("boom")

        result = await with_default_on_failure(
            primary, {"safe": True}, operation_name="t"
        )
        assert result == {"safe": True}


class TestWithFallbackChain:
    async def test_returns_first_successful_stage(self):
        async def s1():
            raise RuntimeError("s1")

        async def s2():
            return "s2"

        async def s3():
            return "s3"

        result = await with_fallback_chain(
            (s1, "s1"),
            (s2, "s2"),
            (s3, "s3"),
        )
        assert result == "s2"

    async def test_returns_default_when_all_stages_fail(self):
        async def s1():
            raise RuntimeError("s1")

        async def s2():
            raise RuntimeError("s2")

        result = await with_fallback_chain(
            (s1, "s1"),
            (s2, "s2"),
            default="safe",
        )
        assert result == "safe"

    async def test_returns_none_when_all_fail_and_no_default(self):
        async def s1():
            raise RuntimeError("s1")

        result = await with_fallback_chain((s1, "s1"))
        assert result is None

    async def test_stages_take_no_arguments(self):
        """
        Stages are called bare; this is the contract for
        pre-bound callables. Passing arguments to the
        chain is intentionally NOT supported — use
        `with_fallback` for that.
        """
        called = []

        async def s1():
            called.append("s1")
            return "ok"

        await with_fallback_chain((s1, "s1"))
        assert called == ["s1"]
