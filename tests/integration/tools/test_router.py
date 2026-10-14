# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the ToolRouter (Full Payload Fan-Out).
"""

from __future__ import annotations

import json
import uuid

import pytest

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.tools.router import ToolRouter

pytestmark = pytest.mark.asyncio


async def test_tool_router_fan_out(clean_redis):
    """
    Test that ToolRouter correctly identifies tool.requested events
    and copies their payload to the correct tool queue.
    """
    router = ToolRouter(clean_redis)
    agent_id = f"a-{uuid.uuid4()}"

    # 1. Create a mix of events
    ignored_event = Event.create(
        event_type="document.received",
        agent_id=agent_id,
        event_class="domain",
        data={"doc": "123"},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )

    tool_req_1 = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "math_doubler", "params": {"number": 10}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )

    tool_req_2 = Event.create(
        event_type="tool.requested",
        agent_id=agent_id,
        event_class="domain",
        data={"tool": "pii_redactor", "params": {"text": "hello"}},
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )

    # 2. Route them
    await router.route_batch([ignored_event, tool_req_1, tool_req_2])

    # 3. Check queues
    # math_doubler queue should have 1 message
    math_q = await clean_redis.xrange("knt:tools:math_doubler:queue")
    assert len(math_q) == 1
    math_payload = json.loads(math_q[0][1][b"payload"].decode())
    assert math_payload["event_id"] == str(tool_req_1.event_id)
    assert math_payload["data"]["params"]["number"] == 10

    # pii_redactor queue should have 1 message
    pii_q = await clean_redis.xrange("knt:tools:pii_redactor:queue")
    assert len(pii_q) == 1
    pii_payload = json.loads(pii_q[0][1][b"payload"].decode())
    assert pii_payload["event_id"] == str(tool_req_2.event_id)
    assert pii_payload["data"]["params"]["text"] == "hello"


# ---------------------------------------------------------------------------
# Dedup-by-event_id (ADR-083)
# ---------------------------------------------------------------------------
#
# End-to-end coverage of the opt-in dedup path. Real Redis
# (not fakeredis) so the SET NX placeholder semantics are
# exercised as production will see them. The backoffice
# 2026-10-14 incident (ADR-013) is the motivating case:
# without dedup, the same ``event_id`` was XADDed 67 times,
# inflating the tool queue and starving the real request
# for 5.5 hours.


class TestDedup:
    """``ToolRouter(clean_redis, dedup=True)`` consults the
    ``(tool_name, event_id)`` SET index BEFORE every ``XADD``
    and SKIPS the write when the index already holds a final
    stream_id. ADR-083 §5.2 (integration tests).
    """

    async def test_dedup_skips_second_route_of_same_event(self, clean_redis):
        """Routing the same event twice with ``dedup=True``
        produces ONE stream entry, not two.
        """
        router = ToolRouter(clean_redis, dedup=True)
        agent_id = f"a-{uuid.uuid4()}"

        event = Event.create(
            event_type="tool.math_doubler.requested",
            agent_id=agent_id,
            event_class="domain",
            data={"params": {"number": 42}},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )

        # Two route batches, same event_id.
        await router.route_batch([event])
        await router.route_batch([event])

        # XRANGE returns 1 entry, not 2 -- the second route was a
        # dedup hit (the SET NX index already held the stream_id).
        math_q = await clean_redis.xrange("knt:tools:math_doubler:queue")
        assert len(math_q) == 1
        first_payload = json.loads(math_q[0][1][b"payload"].decode())
        assert first_payload["event_id"] == str(event.event_id)

    async def test_dedup_does_not_block_distinct_event_ids(self, clean_redis):
        """Two distinct events (different ``event_id``s) produce
        TWO stream entries -- no false-positive dedup hits.
        """
        router = ToolRouter(clean_redis, dedup=True)
        agent_id = f"a-{uuid.uuid4()}"

        e1 = Event.create(
            event_type="tool.math_doubler.requested",
            agent_id=agent_id,
            event_class="domain",
            data={"params": {"number": 1}},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        e2 = Event.create(
            event_type="tool.math_doubler.requested",
            agent_id=agent_id,
            event_class="domain",
            data={"params": {"number": 2}},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )

        await router.route_batch([e1, e2])

        math_q = await clean_redis.xrange("knt:tools:math_doubler:queue")
        assert len(math_q) == 2

    async def test_dedup_idem_key_visible_to_redis(self, clean_redis):
        """ADR-076 + ADR-083 §3.1: the dedup idem-key lives at
        ``knt:tools:<tool>:idem:<event_id>`` (no prefix here).
        After one route, ``EXISTS`` returns 1.
        """
        router = ToolRouter(clean_redis, dedup=True)
        agent_id = f"a-{uuid.uuid4()}"
        event = Event.create(
            event_type="tool.math_doubler.requested",
            agent_id=agent_id,
            event_class="domain",
            data={"params": {"number": 7}},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )

        await router.route_batch([event])

        idem_key = f"knt:tools:math_doubler:idem:{event.event_id}"
        exists = await clean_redis.exists(idem_key)
        assert exists == 1

    async def test_dedup_idem_key_with_prefix(self, clean_redis):
        """ADR-076: with ``key_prefix="acme:"``, the idem-key
        composes via ``namespaced`` so multi-service Redis
        isolation holds for the dedup index too.
        """
        router = ToolRouter(clean_redis, key_prefix="acme-billing:", dedup=True)
        agent_id = f"a-{uuid.uuid4()}"
        event = Event.create(
            event_type="tool.math_doubler.requested",
            agent_id=agent_id,
            event_class="domain",
            data={"params": {"number": 8}},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )

        await router.route_batch([event])

        idem_key = f"acme-billing:knt:tools:math_doubler:idem:{event.event_id}"
        exists = await clean_redis.exists(idem_key)
        assert exists == 1
        # The unprefixed key MUST NOT exist (the prefix isolates).
        unprefixed_key = f"knt:tools:math_doubler:idem:{event.event_id}"
        assert await clean_redis.exists(unprefixed_key) == 0

    async def test_dedup_ttl_short_overrides_default(self, clean_redis):
        """``dedup_ttl_seconds=2`` writes the idem-key with a
        2-second TTL. After waiting past the TTL, a second
        route of the same event_id is NOT skipped (the
        dedup index has expired). Validates the TTL contract.
        """
        import asyncio

        router = ToolRouter(clean_redis, dedup=True, dedup_ttl_seconds=2)
        agent_id = f"a-{uuid.uuid4()}"
        event = Event.create(
            event_type="tool.math_doubler.requested",
            agent_id=agent_id,
            event_class="domain",
            data={"params": {"number": 9}},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )

        await router.route_batch([event])

        # First route: 1 stream entry, idem-key present.
        math_q = await clean_redis.xrange("knt:tools:math_doubler:queue")
        assert len(math_q) == 1
        idem_key = f"knt:tools:math_doubler:idem:{event.event_id}"
        assert await clean_redis.exists(idem_key) == 1

        # Sleep past the TTL. Redis's TTL granularity is 1s,
        # so 2.5s guarantees the key has expired.
        await asyncio.sleep(2.5)

        # Idem-key has expired.
        assert await clean_redis.exists(idem_key) == 0

        # Second route of the same event_id is NOT skipped
        # (the dedup index is gone). Stream grows to 2.
        await router.route_batch([event])
        math_q = await clean_redis.xrange("knt:tools:math_doubler:queue")
        assert len(math_q) == 2

    async def test_dedup_off_default_does_not_write_idem_keys(self, clean_redis):
        """Back-compat: ``dedup=False`` (the default) keeps the
        pre-ADR-083 wire format. No idem-key is written;
        routing the same event twice produces 2 stream entries.
        """
        router = ToolRouter(clean_redis)
        agent_id = f"a-{uuid.uuid4()}"
        event = Event.create(
            event_type="tool.math_doubler.requested",
            agent_id=agent_id,
            event_class="domain",
            data={"params": {"number": 10}},
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )

        await router.route_batch([event])
        await router.route_batch([event])

        math_q = await clean_redis.xrange("knt:tools:math_doubler:queue")
        assert len(math_q) == 2

        # No idem-key was ever written.
        idem_key = f"knt:tools:math_doubler:idem:{event.event_id}"
        assert await clean_redis.exists(idem_key) == 0
