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
    # ``set`` is added to the fixture so the new dedup=True
    # path (ADR-083) has a real ``AsyncMock`` to track
    # ``set`` calls; the test_router.py unit tests pin
    # ``redis.set.assert_not_awaited()`` for the dedup=False
    # path, which requires ``set`` to be an AsyncMock
    # rather than a default MagicMock attribute.
    redis.set = AsyncMock(return_value=True)
    # ``get`` is needed by the dedup helper's phase-1 read;
    # returning ``None`` simulates "no prior write" so the
    # helper runs the full XADD + SET path. Tests that need
    # a finalised stream_id override the per-test mock.
    redis.get = AsyncMock(return_value=None)
    # ``pipeline`` returns a ``Pipeline``-shaped mock so
    # ``claim_tool_queue_slot`` (ADR-083 §3.2) can call
    # ``pipe.xadd``, ``pipe.set``, and ``await
    # pipe.execute()``. ``execute`` is the only async member.
    pipe = MagicMock()
    pipe.xadd = MagicMock(return_value=pipe)
    pipe.set = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[b"1-0"])
    redis.pipeline = MagicMock(return_value=pipe)
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
        redis_mock.xadd = AsyncMock(side_effect=ConnectionError("redis down"))
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


# ---------------------------------------------------------------------------
# Key prefix (ADR-076 / DEBT §2.35)
# ---------------------------------------------------------------------------
#
# The ``key_prefix`` kwarg on ``ToolRouter.__init__`` namespaces the
# per-tool queue key. Two services sharing one Redis with different
# prefixes must not cross-talk -- a canonical form ``tool.<name>.requested``
# event from service A must NOT land in service B's queue.


class TestKeyPrefix:
    """``ToolRouter.route_batch`` writes the per-tool
    ``xadd`` to ``_stream_key(tool_name)`` which composes
    the namespace prefix via :func:`tool_queue_key`. Empty
    ``key_prefix`` keeps the pre-076 wire format
    byte-for-byte; non-empty prefix namespaces every
    stream key the router writes.
    """

    async def test_default_prefix_writes_unprefixed_stream_key(self, redis_mock):
        """Empty ``key_prefix`` (the default) keeps the
        pre-ADR-076 wire format -- ``xadd`` is awaited with
        ``"knt:tools:echo:queue"``.
        """
        router = ToolRouter(redis_mock)
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        stream_key, _ = redis_mock.xadd.await_args.args
        assert stream_key == "knt:tools:echo:queue"

    async def test_prefixed_router_writes_namespaced_stream_key(self, redis_mock):
        """Non-empty ``key_prefix`` namespaces the
        ``xadd`` target -- the canonical multi-service
        use case from ADR-076 §1.1.
        """
        router = ToolRouter(redis_mock, key_prefix="acme-billing:")
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        stream_key, _ = redis_mock.xadd.await_args.args
        assert stream_key == "acme-billing:knt:tools:echo:queue"

    async def test_prefixed_router_namespaces_legacy_form(self, redis_mock):
        """Legacy ``event_type == "tool.requested"`` form
        also flows through the same ``_stream_key`` helper
        so the prefix composes uniformly across both event
        shapes (canonical and legacy).
        """
        router = ToolRouter(redis_mock, key_prefix="acme:")
        event = _make_legacy_requested("agent-1", "echo")
        await router.route_batch([event])
        stream_key, _ = redis_mock.xadd.await_args.args
        assert stream_key == "acme:knt:tools:echo:queue"

    async def test_invalid_prefix_rejected_at_construction(self, redis_mock):
        """``validate_prefix`` runs once at construction;
        a malformed prefix raises ``ValueError`` immediately
        instead of producing malformed Redis keys at
        routing time.
        """
        with pytest.raises(ValueError, match="redis_key_prefix"):
            ToolRouter(redis_mock, key_prefix="acme:{")


# ---------------------------------------------------------------------------
# Dedup (ADR-083)
# ---------------------------------------------------------------------------
#
# ToolRouter gains an opt-in ``dedup=True`` kwarg that mirrors the
# EventLog idempotency pattern (3-phase SET NX placeholder) so a
# reactive wake that re-emits the same ``tool.<name>.requested`` event
# does not balloon the per-tool queue (backoffice 2026-10-14 incident:
# 67 entries with the same event_id, 5.5h latency).
#
# The 8 tests below cover:
#   - opt-out default preserves the legacy wire format
#     (``xadd`` only, no ``set``)
#   - opt-in skips duplicates (happy path)
#   - opt-in passes through distinct event_ids (happy path, negative)
#   - opt-in falls through to bare ``xadd`` on infra error
#   - opt-in idem-key shape (ADR-076 prefix alignment)
#   - opt-in TTL is forwarded to Redis ``set ex=``


class TestDedup:
    """``ToolRouter(..., dedup=True)`` consults a
    ``(tool_name, event_id)`` SET index before ``XADD`` and skips
    the write when the index already holds a final stream_id.
    """

    async def test_dedup_off_default_keeps_legacy_behaviour(self, redis_mock):
        """The default ``dedup=False`` keeps the pre-ADR-083 wire
        format byte-for-byte: ``xadd`` once per event, NO ``set``
        call. Pins the back-compat invariant.
        """
        router = ToolRouter(redis_mock)
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        redis_mock.xadd.assert_awaited_once()
        redis_mock.set.assert_not_awaited()

    async def test_dedup_on_skips_duplicate_event_id(self, redis_mock, caplog):
        """Same event routed twice in one ``route_batch`` call:
        first XADD succeeds (pipeline.execute returns b"1-0"),
        second is a dedup hit (the dedup index already holds
        the stream_id from the first write). The fixture's
        ``redis.get`` returns ``None`` on the first call and
        ``b"1-0"`` on the second (simulating the stateful
        Redis: the finalize phase replaced the placeholder
        with the final stream_id between the two events).
        """
        import logging

        redis_mock.get = AsyncMock(side_effect=[None, b"1-0"])
        router = ToolRouter(redis_mock, dedup=True)
        event = _make_canonical_requested("agent-1", "echo")
        with caplog.at_level(logging.DEBUG, logger="kntgraph.tools._router_dedup"):
            await router.route_batch([event, event])
        # First event wrote the dedup index; second event was
        # a dedup hit (no new pipeline execution for it).
        # The pipeline ran exactly once.
        assert redis_mock.pipeline.call_count == 1
        # The second event was a dedup hit (NOT a transport
        # error); the dedup helper returned Err(dedup_hit)
        # and the router logged "skipped_dedup".
        assert "skipped_dedup" in caplog.text

    async def test_dedup_on_passes_through_distinct_event_ids(self, redis_mock):
        """Two events with distinct event_ids must each produce
        one XADD (no false-positive dedup hits).
        """
        router = ToolRouter(redis_mock, dedup=True)
        e1 = _make_canonical_requested("agent-1", "echo")
        e2 = _make_canonical_requested("agent-1", "ocr")
        await router.route_batch([e1, e2])
        # Two distinct dedup-index keys were set (one per
        # event_id); the XADD path runs once per event.
        idem_keys = {call.args[0] for call in redis_mock.set.await_args_list}
        assert len(idem_keys) == 2
        # The idem keys encode the tool_name (echo vs ocr).
        echo_keys = [k for k in idem_keys if "echo" in k]
        ocr_keys = [k for k in idem_keys if "ocr" in k]
        assert len(echo_keys) == 1
        assert len(ocr_keys) == 1

    async def test_dedup_on_falls_through_on_redis_set_error(self, redis_mock):
        """When the dedup-index ``set`` call raises a transport
        error, the dispatcher MUST fall through to the bare
        ``xadd`` so a transient Redis hiccup never costs the
        dispatcher a real request (best-effort fan-out).
        """
        from redis import exceptions as redis_exceptions

        redis_mock.set = AsyncMock(
            side_effect=redis_exceptions.ConnectionError("set failed")
        )
        router = ToolRouter(redis_mock, dedup=True)
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        # Fall-through: XADD was still called.
        redis_mock.xadd.assert_awaited_once()

    async def test_dedup_idem_key_includes_prefix(self, redis_mock):
        """ADR-076: the dedup idem-key composes the prefix via
        ``namespaced`` so multi-service Redis isolation holds
        for the dedup index too.
        """
        router = ToolRouter(redis_mock, key_prefix="acme-billing:", dedup=True)
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        # At least one ``set`` awaited with a namespaced idem-key.
        set_calls = redis_mock.set.await_args_list
        assert set_calls, "expected dedup to call set"
        first_key = set_calls[0].args[0]
        assert first_key.startswith("acme-billing:knt:tools:echo:idem:")

    async def test_dedup_ttl_passed_to_set(self, redis_mock):
        """The default TTL (``DEDUP_TTL_SECONDS_DEFAULT = 3600``)
        is forwarded to Redis ``set ex=`` so the dedup index
        self-expires.
        """
        router = ToolRouter(redis_mock, dedup=True)
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        # The placeholder-phase SET used ``nx=True`` and ``ex=3600``.
        # The finalize-phase SET also uses ``ex=3600`` for the
        # stream_id value.
        ex_values = [c.kwargs.get("ex") for c in redis_mock.set.await_args_list]
        assert 3600 in ex_values

    async def test_dedup_ttl_override(self, redis_mock):
        """``dedup_ttl_seconds=`` overrides the framework default."""
        router = ToolRouter(redis_mock, dedup=True, dedup_ttl_seconds=120)
        event = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([event])
        ex_values = [c.kwargs.get("ex") for c in redis_mock.set.await_args_list]
        assert 120 in ex_values
        # 3600 should NOT appear because the override wins.
        assert 3600 not in ex_values

    async def test_dedup_disabled_does_not_call_set(self, redis_mock):
        """``dedup=False`` MUST NOT call ``set`` (the cost claim
        in ADR-083 §3.4). One XADD per event.
        """
        router = ToolRouter(redis_mock, dedup=False)
        e1 = _make_canonical_requested("agent-1", "echo")
        e2 = _make_canonical_requested("agent-1", "echo")
        await router.route_batch([e1, e2])
        assert redis_mock.xadd.await_count == 2
        redis_mock.set.assert_not_awaited()
