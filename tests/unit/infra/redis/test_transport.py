# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``infra.redis._transport.is_transport_error``.

The predicate is the single source of truth for transport
classification in the framework (ADR-082 §3.1). The tests
pin:

  - the catch list (every class in
    ``_TRANSPORT_ERRORS`` returns ``True``);
  - the exclusion list (``WatchError``, ``ResponseError``,
    ``DataError`` return ``False``);
  - the substring-match regression: an exception whose
    message body contains ``"closed"`` or ``"transport"``
    but whose class is not in the catch list returns
    ``False``.
"""

from __future__ import annotations

import pytest
from redis import exceptions as redis_exceptions

from kntgraph.infra.redis._transport import is_transport_error


def test_is_transport_error_class_match() -> None:
    """Every class in the predicate's catch list returns
    ``True``."""
    cases: list[BaseException] = [
        redis_exceptions.ConnectionError("x"),
        redis_exceptions.TimeoutError("x"),
        redis_exceptions.BusyLoadingError("LOADING"),
        ConnectionError("x"),
        TimeoutError("x"),
        OSError("broken pipe"),
    ]
    for e in cases:
        assert is_transport_error(e), f"expected True for {type(e).__name__}"


def test_is_transport_error_application_excluded() -> None:
    """``RedisError`` subclasses that indicate a wrong
    command (not a transport failure) return ``False``."""
    cases: list[BaseException] = [
        redis_exceptions.ResponseError("WRONGTYPE"),
        redis_exceptions.WatchError(),
        redis_exceptions.DataError("malformed"),
        ValueError("transport pipeline not configured"),
        KeyError("not a transport failure"),
    ]
    for e in cases:
        assert not is_transport_error(e), (
            f"expected False for {type(e).__name__}: {e!s}"
        )


def test_is_transport_error_substring_does_not_match() -> None:
    """Substring matching on the message body would be
    implicit duck typing on a string the framework does
    not own. The predicate is class-based only — an
    exception whose message contains ``"closed"`` or
    ``"transport"`` but whose class is not in the catch
    list returns ``False``.

    This is the regression test for the substring-match
    footgun the ADR-082 §1.6 row 1 rejected.
    """
    cases: list[BaseException] = [
        ValueError("Connection is closed"),
        RuntimeError("transport not configured"),
        redis_exceptions.DataError("closed during sync"),
    ]
    for e in cases:
        assert not is_transport_error(e), (
            f"expected False for {type(e).__name__} (substring should not match): {e!s}"
        )


def test_is_transport_error_accepts_base_exception_subclass() -> None:
    """The parameter type is ``BaseException``, not
    ``Exception`` — ``SystemExit`` and ``KeyboardInterrupt``
    subclasses are accepted (and return ``False``)."""
    assert not is_transport_error(SystemExit("nope"))
    assert not is_transport_error(KeyboardInterrupt())


@pytest.mark.asyncio
async def test_is_transport_error_usable_in_async_context() -> None:
    """Sanity check: the predicate is sync and cheap to
    call from inside an ``except`` block in an async
    consumer (ADR-082 §3.2 use case)."""

    async def _raise() -> None:
        raise redis_exceptions.ConnectionError("simulated")

    with pytest.raises(redis_exceptions.ConnectionError) as exc_info:
        await _raise()

    assert is_transport_error(exc_info.value)
