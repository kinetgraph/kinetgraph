# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``tools/router.py`` (``ToolRouter``).

Closes the tools/router coverage gap (DEBT §3, 38% → 100%).
The router implements the "Full Payload Fan-Out" strategy
of the Tool Worker Pattern (ADR-036): it inspects each
event in a dispatcher's outgoing batch and forwards any
``tool.<name>.requested`` (canonical) or
``tool.requested`` (legacy, with ``data["tool"]``) event
to the global ``knt:tools:<name>:queue`` stream so the
WorkerManager can pick it up without querying the
agent's EventLog.

The four branches are covered below:

  - Canonical form (``tool.<name>.requested``) is parsed
    via ``parse_tool_event`` and forwarded.
  - Legacy form (``tool.requested`` + ``data["tool"]``)
    is matched directly and forwarded.
  - Non-tool events are silently skipped.
  - Redis errors during ``xadd`` are logged but do not
    crash the caller (the dispatcher loop continues).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.tools.router import ToolRouter


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_mock():
    redis = MagicMock()
    redis.xadd = AsyncMock(return_value=b"1-0")
    return redis


@pytest_asyncio.fixture
async def router(redis_mock):
    return ToolRouter(redis_mock)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_legacy_requested(agent_id: str, tool: str) -> Event:
    return Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": tool, "params": {"text": "hi"}},
        correlation=CorrelationContext.new(),
    )


def _make_canonical_requested(agent_id: str, tool: str) -> Event:
    return Event.create(
        event_type=f"tool.{tool}.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"params": {"text": "hi"}},
        correlation=CorrelationContext.new(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRouteBatch:
    async def test_routes_legacy_form(self, router, redis_mock):
        event = _make_legacy_requested("agent-1", "echo")
        await router.route_batch([event])
        redis_mock.xadd.assert_awaited_once()
        stream_key, fields = redis_mock.xadd.await_args.args
        assert stream_key == "knt:tools:echo:queue"
        assert "payload" in fields

    async def test_routes_canonical_form(self, router, redis_mock):
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        redis_mock.xadd.assert_awaited_once()
        stream_key, _ = redis_mock.xadd.await_args.args
        assert stream_key == "knt:tools:echo:queue"

    async def test_skips_non_tool_events(self, router, redis_mock):
        event = Event.create(
            event_type="document.received",
            agent_id="agent-1",
            event_class="domain",
            data={"doc_id": "NF-001"},
            correlation=CorrelationContext.new(),
        )
        await router.route_batch([event])
        redis_mock.xadd.assert_not_awaited()

    async def test_skips_tool_completed_events(self, router, redis_mock):
        event = Event.create(
            event_type="tool.echo.completed",
            agent_id="agent-1",
            event_class="domain",
            data={"text": "done"},
            correlation=CorrelationContext.new(),
        )
        await router.route_batch([event])
        redis_mock.xadd.assert_not_awaited()

    async def test_skips_legacy_form_without_tool_key(self, router, redis_mock):
        event = Event.create(
            event_type="tool.requested",
            agent_id="agent-1",
            event_class="domain",
            data={"params": {}},  # no "tool" key
            correlation=CorrelationContext.new(),
        )
        await router.route_batch([event])
        redis_mock.xadd.assert_not_awaited()

    async def test_continues_after_xadd_error(self, router, redis_mock, caplog):
        redis_mock.xadd = AsyncMock(side_effect=Exception("redis down"))
        e1 = _make_canonical_requested("agent-1", "echo")
        e2 = _make_canonical_requested("agent-1", "ocr")

        with caplog.at_level(logging.ERROR, logger="kntgraph.tools.router"):
            await router.route_batch([e1, e2])

        # Both xadd calls were attempted; the second
        # was not skipped because the first raised.
        assert redis_mock.xadd.await_count == 2
        assert "redis down" in caplog.text

    async def test_routes_mixed_batch(self, router, redis_mock):
        legacy = _make_legacy_requested("agent-1", "echo")
        canonical = _make_canonical_requested("agent-1", "ocr")
        unrelated = Event.create(
            event_type="document.received",
            agent_id="agent-1",
            event_class="domain",
            data={"doc_id": "NF-001"},
            correlation=CorrelationContext.new(),
        )

        await router.route_batch([legacy, canonical, unrelated])

        assert redis_mock.xadd.await_count == 2
        stream_keys = {call.args[0] for call in redis_mock.xadd.await_args_list}
        assert stream_keys == {
            "knt:tools:echo:queue",
            "knt:tools:ocr:queue",
        }

    async def test_empty_batch_is_noop(self, router, redis_mock):
        await router.route_batch([])
        redis_mock.xadd.assert_not_awaited()

    async def test_payload_contains_event_json(self, router, redis_mock):
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        _, fields = redis_mock.xadd.await_args.args
        # The payload is the JSON-serialised event; it
        # should at least be a string.
        assert isinstance(fields["payload"], str)
        assert "tool.echo.requested" in fields["payload"]
