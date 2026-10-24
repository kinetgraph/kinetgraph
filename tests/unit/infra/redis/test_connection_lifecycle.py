# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Connection-lifecycle contract for the Redis layer.

These tests pin the behaviour that the redis-py documentation
makes explicit and that the framework previously got wrong:

    "auto_close_connection_pool only has an effect if
     connection_pool is None. It is assumed that if
     connection_pool is not None, the user wants to manage the
     connection pool themselves."  (redis-py, ``Redis.__init__``)

``RedisPool`` passes an explicit ``connection_pool``, so
``client.aclose()`` does **not** close the pool. The framework
treated it as if it did — the connections stayed open and the
process leaked them.

The tests split into two groups:

- **Unit** (``TestRedisPoolCloseContract``): the close path is
  exercised against a real ``redis.asyncio`` client built the
  way ``RedisPool`` builds it, so the "does not close" behaviour
  is real rather than mocked. No server required: the pool
  keeps its connections without a live socket until one is
  used.
- **Integration** (``TestConnectionLifecycleIntegration``):
  the same contract against a real Redis server.

Why this matters (the "presas" symptom): a service that
creates one pool per adapter and never closes it holds N
pools x ``max_connections`` sockets for the lifetime of the
process, and a reconnect storm multiplies that further.
"""

from __future__ import annotations

import asyncio

import pytest
import redis.asyncio as redis_async

from kntgraph.infra.redis._pool import RedisPool

pytestmark = pytest.mark.asyncio


def _make_pool(
    url: str = "redis://localhost:6379",
) -> tuple[RedisPool, redis_async.Redis]:
    """Return ``(RedisPool, client)`` with the pool's private
    attributes injected (the dataclass is frozen).

    The client owns an explicit ``connection_pool`` — the exact
    shape ``from_settings`` produces, and the one redis-py does
    NOT auto-close.
    """
    import redis.asyncio as ra

    conn_pool = ra.ConnectionPool.from_url(url, max_connections=50)
    client = ra.Redis(connection_pool=conn_pool)
    wrapper = RedisPool.__new__(RedisPool)
    object.__setattr__(wrapper, "_client", client)
    object.__setattr__(wrapper, "_key_prefix", "")
    return wrapper, client


class _StubWriter:
    """Minimal stand-in for an ``asyncio`` transport writer.

    ``Connection.disconnect`` calls ``_writer.close()`` and then
    ``await _writer.wait_closed()``. The unit tests must run
    without a live server, so the socket is stubbed — but the stub
    has to be honest about *closing*, otherwise the assertion
    "the socket was closed" would be vacuously true.
    """

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _seed_connection(conn_pool) -> object:
    """Put one *connected* connection into the pool without
    touching the network.

    ``ConnectionPool.get_connection`` opens a real socket, so it
    needs a live server (and a password); the unit tests must run
    without one. A ``Connection`` with a stubbed socket is enough:
    the pool keeps it in ``_available_connections`` and the close
    path must close that socket.

    The stub mimics a connected socket (``_reader`` / ``_writer``
    set), so the assertion "the socket was closed" is meaningful
    rather than vacuously true.
    """
    from redis.asyncio import Connection

    conn = Connection()
    conn._reader = object()  # type: ignore[assignment]
    conn._writer = _StubWriter()  # type: ignore[assignment]
    conn_pool._available_connections.append(conn)
    return conn


class TestRedisPoolCloseContract:
    """The close path must actually release the pool's
    connections — not just the client's own view of them.
    """

    async def test_aclose_closes_the_connection_pool(self):
        """``RedisPool.aclose()`` must close the *pool's sockets*.

        redis-py: when ``connection_pool`` is passed explicitly,
        ``client.aclose()`` releases the client's own connection
        but leaves the pool's sockets open. The framework owns the
        pool (it built it in ``from_settings``), so it must close
        it.

        The assertion is on the socket, not on the pool's internal
        bookkeeping: ``pool.aclose()`` closes the socket and keeps
        the ``Connection`` object for reuse, and the object being
        retained is not a leak — the socket it held is gone.
        """
        wrapper, client = _make_pool()
        conn_pool = client.connection_pool

        # Put a connection in the pool so the close has something
        # to release (no server needed — the pool holds the
        # connection object either way).
        conn = _seed_connection(conn_pool)

        await wrapper.aclose()

        assert conn._reader is None and conn._writer is None, (
            "RedisPool.aclose() left the socket open — the "
            "connections leak. redis-py only closes the pool when "
            "the caller owns it; RedisPool built it, so the "
            "framework must close it explicitly."
        )

    async def test_aclose_releases_pooled_connections(self):
        """After ``aclose()`` no socket may remain open.

        The pool keeps ``Connection`` objects between commands; what
        matters is that none of them still holds a socket. If
        ``aclose()`` leaves them connected, every reconnect
        scenario stacks another set of sockets.
        """
        wrapper, client = _make_pool()
        conn_pool = client.connection_pool

        conn = _seed_connection(conn_pool)

        await wrapper.aclose()

        assert conn._reader is None, (
            "a connection is still holding an open socket after aclose()"
        )
        assert len(conn_pool._in_use_connections) == 0, (
            "connections still checked out of the pool after aclose()"
        )

    async def test_aclose_is_idempotent(self):
        """Calling ``aclose()`` twice must not raise.

        Shutdown paths can run twice (an explicit ``stop()``
        plus an ``atexit`` hook, or a retry after a transport
        error already closed it).
        """
        wrapper, _client = _make_pool()

        await wrapper.aclose()
        await wrapper.aclose()  # must not raise

    async def test_aclose_swallows_transport_errors(self):
        """ADR-077: the close path is best-effort. A transport
        failure while closing is logged, not raised — the
        process is shutting down.
        """
        wrapper, client = _make_pool()

        async def _boom(*_args, **_kwargs):
            raise ConnectionError("socket already gone")

        client.aclose = _boom  # type: ignore[method-assign]

        await wrapper.aclose()  # must not raise

    async def test_aclose_propagates_cancellation(self):
        """``asyncio.CancelledError`` must NOT be swallowed.

        ADR-077 scopes the catch narrowly; a cancellation
        arriving during shutdown must propagate so the task
        ends promptly.
        """
        wrapper, client = _make_pool()

        async def _cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError

        client.aclose = _cancelled  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await wrapper.aclose()


class TestSharedPoolOwnership:
    """A pool shared by several adapters must survive one
    adapter closing its transport.
    """

    async def test_close_transport_does_not_break_a_shared_pool(self):
        """``close_transport()`` on one adapter must not leave
        the *shared* pool unusable for the others.

        ``close_transport`` is the ADR-082 reconnect path: the
        intent is "drop the broken socket so the next command
        reconnects", NOT "tear down the pool the other adapters
        are still using".
        """
        from kntgraph.infra.redis._event_log._adapter import RedisEventLogAdapter

        conn_pool = redis_async.ConnectionPool.from_url(
            "redis://localhost:6379", max_connections=50
        )
        client = redis_async.Redis(connection_pool=conn_pool)

        a1 = RedisEventLogAdapter(client=client, key_prefix="svc:")
        # ``a2`` is the sibling adapter that shares the pool; it
        # exists to make the sharing explicit, not to be called.
        _a2 = RedisEventLogAdapter(client=client, key_prefix="svc:")

        _seed_connection(conn_pool)

        await a1.close_transport()

        # The pool must still hold its connection for the sibling.
        assert len(conn_pool._available_connections) == 1, (
            "close_transport() tore down the shared ConnectionPool — "
            "every other adapter on that pool is left with a dead "
            "transport."
        )
        assert _a2.client is client

    async def test_close_transport_closes_the_socket(self):
        """``close_transport()`` must actually close the socket.

        This is the half of the contract the current code misses:
        ``client.aclose()`` releases the connection object back to
        the pool WITHOUT closing the socket, so the next command
        reuses the same (possibly half-open) TCP. The ADR-082
        promise "force the next op to reconnect" is not kept.
        """
        from kntgraph.infra.redis._event_log._adapter import RedisEventLogAdapter

        conn_pool = redis_async.ConnectionPool.from_url(
            "redis://localhost:6379", max_connections=50
        )
        client = redis_async.Redis(connection_pool=conn_pool)
        adapter = RedisEventLogAdapter(client=client, key_prefix="svc:")

        conn = _seed_connection(conn_pool)
        # The socket is open before the close path runs.
        assert conn._reader is not None or conn._writer is not None

        await adapter.close_transport()

        assert conn._reader is None and conn._writer is None, (
            "close_transport() left the socket open — the next "
            "command will reuse the same (possibly half-open) TCP "
            "instead of reconnecting"
        )


class TestFactoryPoolOwnership:
    """The ``create_*`` factories must not leak pools.

    Each factory that builds its own pool must hand the caller
    a way to close it, or share a single pool.
    """

    async def test_factories_require_an_explicit_client(self):
        """A factory called without ``client=`` must refuse.

        The framework cannot build a pool the caller can close: a
        pool created inside the factory is unreachable from
        outside it, so its sockets leak for the life of the
        process (ADR-084 §1.3, §3.2).
        """
        from kntgraph.infra.redis._errors import RedisClientRequiredError
        from kntgraph.infra.redis._factory import (
            create_dlq_storage,
            create_event_log_storage,
            create_session_storage,
        )

        for factory in (
            create_event_log_storage,
            create_session_storage,
            create_dlq_storage,
        ):
            with pytest.raises(RedisClientRequiredError, match="create_redis_pool"):
                factory()

    async def test_factories_share_the_callers_pool(self):
        """Every factory given the same client uses the SAME pool.

        A service that wires the EventLog, the session store and
        the DLQ must hold one pool (not three) for one Redis.
        """
        from kntgraph.infra.redis._factory import (
            create_dlq_storage,
            create_event_log_storage,
            create_session_storage,
        )

        wrapper, client = _make_pool()

        log = create_event_log_storage(client=client)
        session = create_session_storage(client=client)
        dlq = create_dlq_storage(client=client)

        pools = {id(storage.client.connection_pool) for storage in (log, session, dlq)}

        assert len(pools) == 1, (
            f"the factories opened {len(pools)} distinct connection "
            f"pools for one Redis — each is a separate set of "
            f"sockets that nothing can close"
        )
        assert wrapper.client is client
