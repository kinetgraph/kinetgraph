# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for PlannerRole.
"""

from __future__ import annotations

import pytest


from kntgraph.agents.roles import Plan, PlannerRole
from kntgraph.agents.tools import LiteLLMTool

from .._fake_transport import FakeLLMTransport


pytestmark = pytest.mark.asyncio


_PLAN_JSON = """{
  "goal": "add rate limiter",
  "steps": [
    {"name": "design_api", "description": "design", "depends_on": []},
    {"name": "implement", "description": "code", "depends_on": ["design_api"]}
  ],
  "rationale": "incremental",
  "risks": ["edge cases"]
}"""


class TestPlan:
    async def test_parses_valid_json(self):
        transport = FakeLLMTransport()
        transport.queue_response(text=_PLAN_JSON)
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PlannerRole(llm=tool)

        r = await role.plan("add rate limiter")
        assert r.is_ok()
        p: Plan = r.unwrap()
        assert p.goal == "add rate limiter"
        assert len(p.steps) == 2
        assert p.steps[0].name == "design_api"
        assert p.steps[1].depends_on == ["design_api"]
        assert p.rationale == "incremental"
        assert p.risks == ["edge cases"]

    async def test_empty_task_rejected(self):
        transport = FakeLLMTransport()
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PlannerRole(llm=tool)
        r = await role.plan("")
        assert r.is_err()
        assert "empty" in str(r.err_value())

    async def test_invalid_json_returns_parse_error(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="not json")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PlannerRole(llm=tool)
        r = await role.plan("anything")
        assert r.is_err()
        assert "parse_error" in str(r.err_value())

    async def test_context_included_in_prompt(self):
        transport = FakeLLMTransport()
        transport.queue_response(text=_PLAN_JSON)
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PlannerRole(llm=tool)
        await role.plan("task", context="some background info")
        call = transport.calls[0]
        user_msg = call["messages"][-1]["content"]
        assert "task" in user_msg
        assert "some background info" in user_msg
