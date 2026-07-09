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
