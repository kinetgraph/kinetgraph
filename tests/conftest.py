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

import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Tests under ``tests/scripts/`` and
# ``tests/unit/scripts/`` import modules from the
# repo-root ``scripts/`` directory (path-only imports
# like ``from scripts.readme_stats import _version_badge``
# or ``import migrate_principals``). Pytest does not put
# ``scripts/`` on ``sys.path`` by default; without this
# conftest, pytest collection fails with
# ``ModuleNotFoundError`` (and mutmut's broader collection
# trips on the same). Adding ``scripts/`` makes the path-only
# imports work for both the regular ``pytest`` run and the
# ``mutmut run`` invocation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


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
