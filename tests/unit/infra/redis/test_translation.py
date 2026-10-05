# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``infra.redis._translation.translate_redis_call``.

Per ADR-077 §2.3: the catch list lives in ONE place —
this module. Every adapter in the package calls
``translate_redis_call``; the tests here pin the
catch list so a future contributor adding a new
exception category (``redis-py`` releases, etc.) sees
the impact in this file alone.

The tests cover:

- The happy path (``Ok(value)``).
- Each exception in the catch list individually
  (``redis.exceptions.RedisError``, ``ConnectionError``,
  ``TimeoutError``, ``OSError``).
- The fail-open variant
  (``translate_redis_call_fall_back``).
- The structured log payload (event name, key, error).
- The ``asyncio.CancelledError`` propagation
  guarantee (ADR-077 §2.5).
- The constructor-arg forwarding in
  :func:`_build_typed_error` (kwargs the concrete
  class accepts are forwarded; kwargs it does not
  are dropped).
"""

from __future__ import annotations

import asyncio

import pytest
from redis import exceptions as redis_exceptions
from structlog.testing import capture_logs

from kntgraph.infra.redis._errors import (
    MemoryError as RedisMemoryError,
)
from kntgraph.infra.redis._memory._solution import (
    SolutionStoreError,
)
from kntgraph.infra.redis._translation import (
    translate_redis_call,
    translate_redis_call_fall_back,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# translate_redis_call -- happy path
# ---------------------------------------------------------------------------


async def test_translate_redis_call_returns_ok_on_success() -> None:
    async def op() -> bytes:
        return b"hello"

    result = await translate_redis_call(
        op(), op_name="test.success", error_cls=RedisMemoryError
    )
    assert result.is_ok()
    assert result.ok_value() == b"hello"


async def test_translate_redis_call_returns_ok_with_none_value() -> None:
    async def op() -> None:
        return None

    result = await translate_redis_call(
        op(), op_name="test.void", error_cls=RedisMemoryError
    )
    assert result.is_ok()
    assert result.ok_value() is None


# ---------------------------------------------------------------------------
# translate_redis_call -- catch list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory,expected_text",
    [
        (lambda: redis_exceptions.RedisError("redis down"), "redis down"),
        (lambda: redis_exceptions.ConnectionError("conn lost"), "conn lost"),
        (lambda: redis_exceptions.TimeoutError("redis timeout"), "redis timeout"),
        (lambda: ConnectionError("builtin-conn"), "builtin-conn"),
        (lambda: TimeoutError("builtin-timeout"), "builtin-timeout"),
        (lambda: OSError("disk full"), "disk full"),
    ],
    ids=[
        "redis.RedisError",
        "redis.ConnectionError",
        "redis.TimeoutError",
        "builtin.ConnectionError",
        "builtin.TimeoutError",
        "OSError",
    ],
)
async def test_translate_redis_call_catches_each_listed_exception(
    exc_factory, expected_text
) -> None:
    """Per ADR-077 §2.3: every third-party transport
    failure mode the framework knows about is
    translated to ``Err(error_cls(...))``."""

    async def op() -> None:
        raise exc_factory()

    result = await translate_redis_call(
        op(),
        op_name="test.catch_list",
        error_cls=RedisMemoryError,
        key="knt:test:1",
    )
    assert result.is_err()
    err = result.err_value()
    assert isinstance(err, RedisMemoryError)
    assert err.key == "knt:test:1"
    assert expected_text in str(err)


async def test_translate_redis_call_propagates_cancelled_error() -> None:
    """Per ADR-077 §2.5: ``asyncio.CancelledError``
    propagates; the framework's cancellation
    contract is preserved."""

    async def op() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await translate_redis_call(
            op(),
            op_name="test.cancelled",
            error_cls=RedisMemoryError,
        )


async def test_translate_redis_call_does_not_catch_base_exception() -> None:
    """``KeyboardInterrupt`` and ``SystemExit`` are
    ``BaseException`` subclasses; they propagate
    through ``except Exception`` (which is what the
    helper uses). This is the second half of the
    ADR-077 §2.5 guarantee.
    """

    async def op() -> None:
        raise KeyboardInterrupt("operator stop")

    with pytest.raises(KeyboardInterrupt):
        await translate_redis_call(
            op(),
            op_name="test.keyboard_interrupt",
            error_cls=RedisMemoryError,
        )


# ---------------------------------------------------------------------------
# translate_redis_call -- structured log payload
# ---------------------------------------------------------------------------


async def test_translate_redis_call_emits_structured_log_on_error() -> None:
    async def op() -> None:
        raise redis_exceptions.RedisError("boom")

    with capture_logs() as caplog:
        result = await translate_redis_call(
            op(),
            op_name="my_op.redis_error",
            error_cls=RedisMemoryError,
            key="knt:cache:abc",
        )

    assert result.is_err()
    # Exactly one warning was emitted.
    assert len(caplog) == 1
    record = caplog[0]
    assert record["event"] == "my_op.redis_error.redis_error"
    assert record["key"] == "knt:cache:abc"
    assert record["log_level"] == "warning"
    assert "boom" in record["error"]


async def test_translate_redis_call_preserves_extra_log_ctx() -> None:
    async def op() -> None:
        raise redis_exceptions.RedisError("boom")

    with capture_logs() as caplog:
        await translate_redis_call(
            op(),
            op_name="my_op.redis_error",
            error_cls=RedisMemoryError,
            key="k",
            tool_name="weather",
            params_fingerprint="abc123",
        )

    assert len(caplog) == 1
    record = caplog[0]
    assert record["tool_name"] == "weather"
    assert record["params_fingerprint"] == "abc123"


# ---------------------------------------------------------------------------
# translate_redis_call -- concrete error class
# ---------------------------------------------------------------------------


async def test_translate_redis_call_uses_caller_error_cls() -> None:
    async def op() -> None:
        raise redis_exceptions.RedisError("boom")

    result = await translate_redis_call(
        op(),
        op_name="sol.put",
        error_cls=SolutionStoreError,
        tool_name="weather",
    )
    err = result.err_value()
    assert isinstance(err, SolutionStoreError)
    assert err.tool_name == "weather"
    # ``key`` is NOT in the SolutionStoreError constructor;
    # the helper's introspect-by-trying must drop it
    # from the kwargs before constructing the error.
    assert not hasattr(err, "key")


async def test_translate_redis_call_forwards_key_to_memory_error() -> None:
    async def op() -> None:
        raise redis_exceptions.RedisError("boom")

    result = await translate_redis_call(
        op(),
        op_name="mem.get",
        error_cls=RedisMemoryError,
        key="knt:cache:abc",
    )
    err = result.err_value()
    assert err.key == "knt:cache:abc"


async def test_translate_redis_call_forwards_only_accepted_kwargs() -> None:
    """A subclass that does NOT accept ``key=`` (e.g.
    a hypothetical vertical that only takes a
    ``request_id=``) must still build correctly when
    the helper is called with the canonical memory-tier
    ``key=`` kwarg. The introspection-by-trying path
    retries without the unsupported kwargs.
    """

    class _OnlyRequestIdError(RedisMemoryError):
        """Vertical error that takes only ``request_id=``.
        Mirrors how a vertical that has not yet adopted
        the ``RedisAdapterError`` key convention would
        behave."""

        def __init__(self, message: str, *, request_id: str) -> None:
            super().__init__(message, key=None)
            self.request_id = request_id

    async def op() -> None:
        raise redis_exceptions.RedisError("boom")

    # No exception should propagate; the helper
    # builds the error successfully.
    result = await translate_redis_call(
        op(),
        op_name="test.kwarg_filter",
        error_cls=_OnlyRequestIdError,
        key="knt:cache:abc",
        request_id="req-123",
    )
    err = result.err_value()
    assert isinstance(err, _OnlyRequestIdError)
    assert err.request_id == "req-123"


# ---------------------------------------------------------------------------
# translate_redis_call_fall_back
# ---------------------------------------------------------------------------


async def test_translate_redis_call_fall_back_returns_value_on_success() -> None:
    async def op() -> bytes:
        return b"value"

    result = await translate_redis_call_fall_back(
        op(),
        op_name="sol.find_match",
        fallback=None,
    )
    assert result == b"value"


async def test_translate_redis_call_fall_back_returns_fallback_on_redis_error() -> None:
    async def op() -> bytes:
        raise redis_exceptions.RedisError("boom")

    result = await translate_redis_call_fall_back(
        op(),
        op_name="sol.find_match",
        fallback=None,
        key="knt:cache:abc",
    )
    assert result is None


async def test_translate_redis_call_fall_back_returns_dict_fallback() -> None:
    """Per ADR-049 §2.1.3 the read-side Solution store
    fails open with an empty dict on Redis error."""

    async def op() -> dict:
        raise redis_exceptions.RedisError("boom")

    result = await translate_redis_call_fall_back(
        op(),
        op_name="sol.read_all",
        fallback={},
    )
    assert result == {}


async def test_translate_redis_call_fall_back_propagates_cancelled_error() -> None:
    async def op() -> bytes:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await translate_redis_call_fall_back(
            op(),
            op_name="test.cancelled",
            fallback=None,
        )


async def test_translate_redis_call_fall_back_emits_warning() -> None:
    async def op() -> bytes:
        raise redis_exceptions.RedisError("redis down")

    with capture_logs() as caplog:
        result = await translate_redis_call_fall_back(
            op(),
            op_name="sol.find_match",
            fallback=None,
            key="knt:cache:abc",
            tool_name="weather",
        )

    assert result is None
    assert len(caplog) == 1
    record = caplog[0]
    assert record["event"] == "sol.find_match.redis_error"
    assert record["key"] == "knt:cache:abc"
    assert record["tool_name"] == "weather"
    assert "redis down" in record["error"]


# ---------------------------------------------------------------------------
# Constructor-args introspection (regression for SolutionStoreError shape)
# ---------------------------------------------------------------------------


async def test_solution_store_error_constructor_introspected() -> None:
    """``SolutionStoreError.__init__`` takes
    ``tool_name=`` and ``params_fingerprint=``, not
    ``key=``. The helper must NOT raise when the
    caller passes ``key=`` (which is the convention
    in the memory tiers).
    """

    async def op() -> bytes:
        raise redis_exceptions.RedisError("boom")

    # No exception should propagate; the helper
    # builds the error successfully.
    result = await translate_redis_call(
        op(),
        op_name="sol.put",
        error_cls=SolutionStoreError,
        key="knt:solution:weather:abc",
        tool_name="weather",
        params_fingerprint="abc123",
    )
    err = result.err_value()
    assert isinstance(err, SolutionStoreError)
    assert err.tool_name == "weather"
    assert err.params_fingerprint == "abc123"
