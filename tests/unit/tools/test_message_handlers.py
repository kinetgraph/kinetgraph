# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``tools._message_handlers.reconnect_redis_after_*_error``.

The helper exists to break a tight reconnect loop on the
consume path: disconnect the pool's sockets so the next
``xreadgroup`` reconnects to a fresh connection. ADR-082 §3.1
ratifies it; ADR-082 §3.3 deprecates the old ``runtime_error``
name in favour of ``transport_error``.

ADR-084 §3.3 narrowed the implementation: the client's
``aclose()`` releases the connection object back to the pool
WITHOUT closing its socket, so the next command would reuse
the same (possibly half-open) TCP. Only ``pool.disconnect()``
closes the socket, and it leaves the pool usable for the
sibling consumers.

The tests pin:

  - the deprecation warning fires and the old name still
    works (one minor of overlap before the v0.19 ``git rm``);
  - the canonical helper accepts a ``RedisLike`` and
    returns silently when the object lacks ``connection_pool``;
  - the canonical helper calls ``pool.disconnect`` and does
    NOT call the client's ``aclose``.
"""

from __future__ import annotations

import pytest

from kntgraph.tools._message_handlers import (
    reconnect_redis_after_runtime_error,
    reconnect_redis_after_transport_error,
)

pytestmark = pytest.mark.asyncio


async def test_reconnect_redis_after_runtime_error_warns() -> None:
    """The old name forwards to the new helper and emits
    ``DeprecationWarning`` with the v0.19 removal target in
    the message (ADR-082 §3.3).
    """

    class _FakeRedis:
        async def aclose(self) -> None:
            pass

        @property
        def connection_pool(self) -> None:
            return None

    with pytest.warns(DeprecationWarning, match="v0.19"):
        await reconnect_redis_after_runtime_error(_FakeRedis())  # type: ignore[arg-type]


async def test_reconnect_redis_after_transport_error_signature() -> None:
    """The canonical helper accepts a ``RedisLike`` and
    returns silently when the object lacks both ``aclose``
    and ``connection_pool`` (best-effort, no raise)."""

    class _Bare:
        pass

    bare = _Bare()
    await reconnect_redis_after_transport_error(bare)  # type: ignore[arg-type]


async def test_reconnect_redis_after_transport_error_accepts_redis_like() -> None:
    """The canonical helper calls ``pool.disconnect`` on a
    ``RedisLike`` that exposes it.

    ADR-084 §3.3 narrowed this path: the client's ``aclose()``
    releases the connection object back to the pool WITHOUT
    closing the socket, so the next command reuses the same
    (possibly half-open) TCP and the ADR-082 promise "force the
    next op to reconnect" is not kept. ``pool.disconnect()``
    closes the socket and leaves the pool usable for the sibling
    consumers.
    """

    class _FakePool:
        def __init__(self) -> None:
            self.disconnect_called = False

        def disconnect(self, inuse_connections: bool = True) -> None:
            self.disconnect_called = True

    class _FakeRedis:
        def __init__(self) -> None:
            self.aclose_called = False
            self.pool = _FakePool()

        async def aclose(self) -> None:
            self.aclose_called = True

        @property
        def connection_pool(self) -> _FakePool:
            return self.pool

    fake = _FakeRedis()
    await reconnect_redis_after_transport_error(fake)  # type: ignore[arg-type]
    assert fake.pool.disconnect_called
    # ``aclose()`` is deliberately NOT called: it would release the
    # connection object without closing its socket.
    assert not fake.aclose_called


async def test_reconnect_redis_after_transport_error_swallows_close_failure() -> None:
    """A failure on the close path is swallowed; the
    helper returns without raising (ADR-077 §3.4)."""

    class _ExplodingRedis:
        async def aclose(self) -> None:
            raise ConnectionError("close failed")

        @property
        def connection_pool(self) -> None:
            return None

    await reconnect_redis_after_transport_error(_ExplodingRedis())  # type: ignore[arg-type]
