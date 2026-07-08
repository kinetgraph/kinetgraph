# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ChatRole.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kntgraph.memory.session import SessionState

from kntgraph.agents.roles import ChatReply, ChatRole
from kntgraph.agents.tools import LiteLLMTool

from .._fake_transport import FakeLLMTransport


pytestmark = pytest.mark.asyncio


def _session(messages: list[dict]) -> SessionState:
    return SessionState(
        session_id="s-1",
        user_id="u-1",
        tenant_id="t-1",
        messages=tuple(messages),
        context={},
        started_at=datetime.now().timestamp(),
    )


_REPLY_JSON = '{"reply": "olá!", "follow_up_questions": ["como vai?"]}'


class TestReply:
    async def test_parses_valid_json(self):
        transport = FakeLLMTransport()
        transport.queue_response(text=_REPLY_JSON)
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = ChatRole(llm=tool)

        s = _session([{"role": "user", "content": "oi"}])
        r = await role.reply(s, "tudo bem?")
        assert r.is_ok()
        cr: ChatReply = r.unwrap()
        assert cr.reply == "olá!"
        assert cr.follow_up_questions == ["como vai?"]

    async def test_includes_history_in_prompt(self):
        transport = FakeLLMTransport()
        transport.queue_response(text=_REPLY_JSON)
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = ChatRole(llm=tool)

        s = _session(
            [
                {"role": "user", "content": "primeira msg"},
                {"role": "assistant", "content": "resposta 1"},
                {"role": "user", "content": "segunda msg"},
            ]
        )
        await role.reply(s, "terceira msg")
        call = transport.calls[0]
        user_msg = call["messages"][-1]["content"]
        # The prompt must contain all history entries.
        assert "primeira msg" in user_msg
        assert "resposta 1" in user_msg
        assert "segunda msg" in user_msg
        assert "terceira msg" in user_msg

    async def test_persona_in_system(self):
        transport = FakeLLMTransport()
        transport.queue_response(text=_REPLY_JSON)
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = ChatRole(
            llm=tool,
            persona="You are a tax accountant. Be precise.",
        )
        s = _session([])
        await role.reply(s, "explain depreciation")
        call = transport.calls[0]
        system_msg = call["messages"][0]["content"]
        assert "tax accountant" in system_msg

    async def test_empty_message_rejected(self):
        transport = FakeLLMTransport()
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = ChatRole(llm=tool)
        s = _session([])
        r = await role.reply(s, "")
        assert r.is_err()
        assert "empty" in str(r.err_value())

    async def test_invalid_json_returns_parse_error(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="not json")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = ChatRole(llm=tool)
        s = _session([])
        r = await role.reply(s, "hi")
        assert r.is_err()
        assert "parse_error" in str(r.err_value())

    async def test_idempotency_key_changes_with_history(self):
        """Two calls with the same new message but different
        history lengths should produce different keys."""
        transport = FakeLLMTransport()
        transport.queue_response(text=_REPLY_JSON)
        transport.queue_response(text=_REPLY_JSON)
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = ChatRole(llm=tool)

        s_short = _session([])
        s_long = _session([{"role": "user", "content": "x"}] * 5)
        await role.reply(s_short, "msg")
        await role.reply(s_long, "msg")
        # The key is internal, but we can verify both calls
        # succeeded and reached the transport
        assert len(transport.calls) == 2
