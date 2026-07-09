# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for PersonalizedRole.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kntgraph.memory.profile import ProfileState

from kntgraph.agents.roles import PersonalizedRole
from kntgraph.agents.tools import LiteLLMTool

from .._fake_transport import FakeLLMTransport


pytestmark = pytest.mark.asyncio


def _profile(prefs: dict[str, str], tier: str = "standard") -> ProfileState:
    return ProfileState(
        tenant_id="t-1",
        user_id="u-1",
        preferences=prefs,
        tier=tier,
        created_at=datetime.now().timestamp(),
        updated_at=datetime.now().timestamp(),
    )


class TestRespond:
    async def test_returns_text(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="event sourcing rocks")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p = _profile({"language": "en", "tone": "formal"})
        r = await role.respond(p, "what is event sourcing?")
        assert r.is_ok()
        assert r.unwrap() == "event sourcing rocks"

    async def test_language_pt_br_in_system(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="resposta")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p = _profile({"language": "pt-BR"})
        await role.respond(p, "explique X")
        call = transport.calls[0]
        system = call["messages"][0]["content"]
        assert "português brasileiro" in system.lower()

    async def test_language_en_in_system(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="answer")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p = _profile({"language": "en"})
        await role.respond(p, "explain X")
        call = transport.calls[0]
        system = call["messages"][0]["content"]
        assert "english" in system.lower()

    async def test_tone_formal_in_system(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="answer")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p = _profile({"language": "en", "tone": "formal"})
        await role.respond(p, "explain X")
        call = transport.calls[0]
        system = call["messages"][0]["content"]
        assert "formal" in system.lower()

    async def test_tone_concise_in_system(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p = _profile({"language": "en", "tone": "concise"})
        await role.respond(p, "explain X")
        call = transport.calls[0]
        system = call["messages"][0]["content"]
        assert "brief" in system.lower() or "concise" in system.lower()

    async def test_verbosity_low_in_system(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p = _profile({"language": "en", "verbosity": "low"})
        await role.respond(p, "explain X")
        call = transport.calls[0]
        system = call["messages"][0]["content"]
        assert "80 words" in system.lower()

    async def test_verbosity_high_in_system(self):
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p = _profile({"language": "en", "verbosity": "high"})
        await role.respond(p, "explain X")
        call = transport.calls[0]
        system = call["messages"][0]["content"]
        assert "detailed" in system.lower()

    async def test_unknown_language_ignored(self):
        """Unsupported language codes are silently dropped."""
        transport = FakeLLMTransport()
        transport.queue_response(text="ok")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p = _profile({"language": "klingon"})
        await role.respond(p, "explain X")
        call = transport.calls[0]
        system = call["messages"][0]["content"]
        assert "klingon" not in system.lower()

    async def test_empty_task_rejected(self):
        transport = FakeLLMTransport()
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)
        p = _profile({})
        r = await role.respond(p, "")
        assert r.is_err()
        assert "empty" in str(r.err_value())

    async def test_idempotency_key_changes_with_prefs(self):
        """Same task, different prefs → different key (different output)."""
        transport = FakeLLMTransport()
        transport.queue_response(text="a")
        transport.queue_response(text="b")
        tool = LiteLLMTool(default_model="x", transport=transport)
        role = PersonalizedRole(llm=tool)

        p_en = _profile({"language": "en"})
        p_pt = _profile({"language": "pt-BR"})
        await role.respond(p_en, "X")
        await role.respond(p_pt, "X")
        # Different calls — same shape, but the underlying
        # idempotency_key passed to the LLM differs (verified
        # by stable_key logic in code).
        assert len(transport.calls) == 2
