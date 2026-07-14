# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration and fixtures.

This conftest provides two autouse fixtures that the test
suite depends on:

  - ``reset_correlation_context`` (autouse, async): sets
    a default ``CorrelationContext`` under
    ``correlation_middleware`` for the test body. Tests
    that need a specific flow id can either call
    ``correlation_middleware.scope(...)`` directly or use
    a fixture that sets one.
  - ``reset_settings_cache`` (autouse, sync): clears the
    ``fresh_settings`` ``lru_cache`` between tests so a
    ``monkeypatch.setenv(...)`` in test N does not leak
    into test N+1 via the cached singleton.
"""

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def reset_correlation_context():
    """Reset correlation context between tests.

    Sets a default CorrelationContext for the test body so
    call sites that read ``correlation_middleware.current()``
    (memory/profile, memory/session, memory/continuity/manager,
    memory/continuity/recorders/*) get a non-None context under
    ADR-037. Tests that want to assert on a specific flow id
    should call ``correlation_middleware.scope(...)`` directly.
    """
    from uuid import uuid4

    from kntgraph.core.event import CorrelationContext, correlation_middleware
    from kntgraph.core.event.correlation import _correlation_context

    ctx = CorrelationContext.new(correlation_id=uuid4())
    _correlation_context.set(ctx)
    yield
    correlation_middleware.clear()


@pytest.fixture(autouse=True)
def reset_settings_cache():
    """
    Drop the `fresh_settings` lru_cache between tests so a
    `monkeypatch.setenv(...)` in test N does not leak into
    test N+1 via the cached singleton.
    """
    from kntgraph.infra.config import fresh_settings

    fresh_settings.cache_clear()
    yield
    fresh_settings.cache_clear()
