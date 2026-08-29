# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
In-memory ``WorkerManager`` builder for API unit tests.

The HTTP gateway tests need a ``WorkerManager`` only
for ``list_descriptors()`` (the ``GET /agents/{id}/tools``
endpoint) and for ``register`` (the boot path). The
runtime consume loop / process pool / Redis consumer
group are never exercised in these tests, so the
``redis`` and ``event_log`` arguments are mocks.

Usage
-----

    from ._fake_worker_manager import build_fake_manager

    manager = build_fake_manager()
    descriptors = manager.list_descriptors()
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kntgraph.tools.manager import WorkerManager
from kntgraph.tools.worker import tool_worker


@tool_worker(name="fake.echo", description="Echoes the input.")
class _FakeEchoWorker:
    """Minimal ``@tool_worker`` for API tests.

    The body never runs (the tests exercise the HTTP
    gateway, not the worker dispatch). The
    ``@tool_worker`` decorator injects ``name`` /
    ``description`` / ``input_schema`` so
    ``WorkerManager.list_descriptors`` produces a
    valid ``ToolDescriptor``.
    """

    async def invoke(self, *, idempotency_key: str, **kwargs: Any) -> Any:
        raise NotImplementedError  # pragma: no cover


def build_fake_manager(
    *,
    register_echo: bool = True,
) -> WorkerManager:
    """Build a ``WorkerManager`` backed by mocks.

    The manager is NOT started (no consume loop, no
    process pool). It is suitable for
    ``list_descriptors()`` / ``acl_for()`` / ``get()``
    reads and for ``register(...)`` writes.
    """
    redis_mock = MagicMock()
    redis_mock.xack = AsyncMock()
    event_log_mock = MagicMock()
    event_log_mock.append = AsyncMock()
    manager = WorkerManager(redis=redis_mock, event_log=event_log_mock)
    if register_echo:
        manager.register(_FakeEchoWorker, acl=None)
    return manager
