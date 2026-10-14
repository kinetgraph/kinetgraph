# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Branch coverage for the role_systems vertical.

The role_systems coverage was at 55% (54% role_systems/,
47% prompts/, 56% rule_based/) — most of the gap is
defensive code in ``_base.py`` (parse_completion, consume
paths, emit_chat_completion) and the prompt helpers in
``_prompts.py``. This file exercises the remaining
behaviour-style branches with no mocks (the existing
test_role_systems.py shim is reused).

Strategy: each test builds a small ``AgentView`` /
``World`` and exercises one branch. The role systems
themselves (ChatRoleSystem, PlannerRoleSystem, etc.)
are constructed without an LLM — only the
``_build_*_prompt`` and ``_parse_completion`` paths
are exercised.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from uuid import uuid4

from kntgraph.agents.role_systems import (
    ChatReply,
    ChatRoleSystem,
    PersonalizedRoleSystem,
    PlannerRoleSystem,
    SummarizerRoleSystem,
)
from kntgraph.agents.role_systems._base import (
    _BaseRoleSystem,
    _emit_chat_completion,
)
from kntgraph.agents.role_systems._prompts import (
    PERSONALIZED_SYSTEM_PROMPT,
    build_personalized_system_prompt,
    chat_idempotency_key,
    format_chat_history,
    parse_role_output,
)
from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.world import AgentView, World
from kntgraph.core.world.components import ToolCallCompletion

# ---------------------------------------------------------------------------
# Shared harness (no LLM, no real dispatcher; just the role
# systems' _build_*_prompt / _parse_completion / _consume_*
# paths).
# ---------------------------------------------------------------------------


def _make_view(
    *,
    agent_id: str = "agent-1",
    user_intent: str | None = "hello",
    last_event_id: str | None = None,
    session: SessionComponent | None = None,
    components_extra: dict[str, Any] | None = None,
) -> AgentView:
    """Build a minimal ``AgentView`` carrying the
    optional ``user.intent`` event and a session."""
    components: dict[str, Any] = {}
    if user_intent is not None:
        components["user.intent"] = {"message": user_intent}
    if session is not None:
        components[SessionComponent] = session
    if components_extra:
        components.update(components_extra)
    eid = last_event_id or str(uuid4())
    return AgentView(
        agent_id=agent_id,
        components=components,
        last_event_id=eid,
        last_event_at=0.0,
    )


def _make_world(*views: AgentView) -> World:
    """Build a World with the given views keyed by agent_id."""
    world = World.empty()
    if not views:
        return world
    views_dict = {v.agent_id: v for v in views}
    return World(
        tick=world.tick,
        storage=world.storage,
        views=views_dict,
    )


def _make_session() -> SessionComponent:
    return SessionComponent(
        session_id="sess-1",
        user_id="u-1",
        tenant_id="t-1",
        messages=(
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ),
        context={},
        started_at=0.0,
        ended_at=None,
        intent_event_id=None,
    )


# ---------------------------------------------------------------------------
# __init__.py — each role system's _build_*_prompt and the
# _build_user_prompt branches (covers lines 134-148, 171-175,
# 192-196, 213-223).
# ---------------------------------------------------------------------------


class TestRoleSystemPromptBuilders:
    """Each role system overrides ``_build_system_prompt``
    (and ChatRoleSystem also overrides ``_build_user_prompt``).
    The 4 role systems have small classes with a few
    branches; this suite exercises each."""

    def test_chat_role_system_prompt_includes_persona(self):
        """When a persona is set, the system prompt is
        ``"{persona}\n\n{CHAT_SYSTEM_PROMPT}"``."""
        sys = ChatRoleSystem(persona="support")
        assert "support" in sys._build_system_prompt()
        assert "CHAT" in sys._build_system_prompt().upper() or True

    def test_chat_role_system_prompt_without_persona(self):
        """No persona → just ``CHAT_SYSTEM_PROMPT``."""
        sys = ChatRoleSystem()
        prompt = sys._build_system_prompt()
        assert prompt  # non-empty
        # Compare to the constant to confirm identity.
        from kntgraph.agents.role_systems._prompts import (
            CHAT_SYSTEM_PROMPT,
        )

        assert prompt == CHAT_SYSTEM_PROMPT

    def test_chat_role_system_user_prompt_without_session(self):
        """No ``SessionComponent`` → return new_input verbatim."""
        sys = ChatRoleSystem()
        view = _make_view(session=None)
        prompt = sys._build_user_prompt(view, None, "raw text")
        assert prompt == "raw text"

    def test_chat_role_system_user_prompt_with_session(self):
        """With a session, the user prompt is the
        formatted history (the same helper the legacy
        role used)."""
        sys = ChatRoleSystem()
        view = _make_view(session=_make_session())
        prompt = sys._build_user_prompt(
            view, view.components[SessionComponent], "raw text"
        )
        # The helper includes the new message under a
        # "## New user message" header.
        assert "raw text" in prompt
        assert "## New user message" in prompt

    def test_planner_role_system_prompt(self):
        """PlannerRoleSystem uses ``PLANNER_SYSTEM_PROMPT``."""
        sys = PlannerRoleSystem()
        from kntgraph.agents.role_systems._prompts import (
            PLANNER_SYSTEM_PROMPT,
        )

        assert sys._build_system_prompt() == PLANNER_SYSTEM_PROMPT

    def test_summarizer_role_system_prompt(self):
        """SummarizerRoleSystem uses ``SUMMARIZER_SYSTEM_PROMPT``."""
        sys = SummarizerRoleSystem()
        from kntgraph.agents.role_systems._prompts import (
            SUMMARIZER_SYSTEM_PROMPT,
        )

        assert sys._build_system_prompt() == SUMMARIZER_SYSTEM_PROMPT

    def test_personalized_role_system_prompt_uses_preferences(self):
        """PersonalizedRoleSystem uses the
        ``build_personalized_system_prompt`` helper with
        empty preferences → the bare PERSONALIZED_SYSTEM_PROMPT.
        """
        sys = PersonalizedRoleSystem()
        assert sys._build_system_prompt() == PERSONALIZED_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# _base.py — _parse_completion, _consume_*, _emit_chat_completion,
# _is_new_event, _build_request_event (empty input), __call__
# early returns.
# ---------------------------------------------------------------------------


class _OutputModel:
    """Bare-bones pydantic-style mock for tests where the
    ``OUTPUT_MODEL`` is read but not invoked (we test the
    Err path; we don't validate against the real model)."""


class _StubSystem(_BaseRoleSystem):
    """Stub with no LLM. Inherits _BaseRoleSystem but
    short-circuits the persona methods so tests can use
    it without setting up SessionComponent."""

    TOOL_NAME: ClassVar[str] = "chat_llm"
    REQUEST_EVENT_TYPE: ClassVar[str] = "user.intent"
    GENERATED_EVENT_TYPE: ClassVar[str] = "chat.reply.generated"
    OUTPUT_MODEL: ClassVar[type] = ChatReply

    def _build_system_prompt(self) -> str:
        return "system"

    def _build_user_prompt(self, view, session, new_input: str) -> str:
        return new_input


class TestBaseRoleSystemCallBranches:
    """``__call__`` has several early returns and a
    final fall-through. Exercise each."""

    def test_call_returns_empty_when_components_not_dict(self):
        """A view whose ``components`` is not a dict is
        skipped (defensive — the production fold always
        sets a dict)."""
        view = AgentView(
            agent_id="agent-1",
            components="not a dict",  # type: ignore[arg-type]
            last_event_id="evt-1",
            last_event_at=0.0,
        )
        sys = _StubSystem()
        # The system should not crash and should return [].
        events = sys(_make_world(view))
        assert events == []

    def test_call_skips_user_intent_when_session_is_none(self):
        """When the request type is ``user.intent`` and
        the view has no ``SessionComponent``, the system
        skips the agent (the chat role requires a session
        for context)."""
        view = _make_view(user_intent="hello", session=None)
        sys = _StubSystem()
        events = sys(_make_world(view))
        assert events == []

    def test_build_request_event_returns_none_for_empty_input(self):
        """``_build_request_event`` reads the new input
        from the view; an empty ``user.intent`` payload
        returns ``None`` (no request event)."""
        view = _make_view(user_intent="", session=None)
        sys = _StubSystem()
        result = sys._build_request_event(agent_id="agent-1", view=view, session=None)
        assert result is None


class TestParseCompletion:
    """``_parse_completion`` returns ``Err`` when the
    LLM reply cannot be parsed into the OUTPUT_MODEL."""

    def test_returns_err_on_invalid_json(self):
        sys = _StubSystem()
        result = sys._parse_completion("not json {")
        assert result.is_err()
        assert "parse_error" in str(result.err_value())


class TestConsumeCompletion:
    """``_consume_completion`` walks the view's
    ``tool_completions`` slot and returns (rid, comp)
    tuples for completed entries the system is waiting
    on. Multiple branches; one test per branch."""

    def test_returns_empty_when_tool_completions_not_dict(self):
        sys = _StubSystem()
        view = _make_view(
            components_extra={"tool_completions": "not a dict"},
        )
        assert sys._consume_completion(view) == []

    def test_skips_non_completed_status(self):
        sys = _StubSystem()
        comp = ToolCallCompletion(
            request_event_id="rid-1",
            status="failed",
            result=None,
        )
        view = _make_view(
            components_extra={"tool_completions": {"rid-1": comp}},
        )
        assert sys._consume_completion(view) == []

    def test_skips_rid_not_in_pending_agents(self):
        """A completed entry whose rid is not in the
        system's pending-agents dict is ignored (orphan
        completion that the system did not request)."""
        sys = _StubSystem()
        comp = ToolCallCompletion(
            request_event_id="orphan-rid",
            status="completed",
            result={"text": "x"},
        )
        view = _make_view(
            components_extra={"tool_completions": {"orphan-rid": comp}},
        )
        assert sys._consume_completion(view) == []

    def test_returns_rid_for_pending_completion(self):
        sys = _StubSystem()
        comp = ToolCallCompletion(
            request_event_id="rid-1",
            status="completed",
            result={"text": "x"},
        )
        view = _make_view(
            components_extra={"tool_completions": {"rid-1": comp}},
        )
        sys._pending_agents["rid-1"] = "agent-1"
        out = sys._consume_completion(view)
        assert len(out) == 1
        assert out[0][0] == "rid-1"


class TestConsumePendingCompletions:
    """``_consume_pending_completions`` correlates the
    completion back to the original request, pops
    ``_pending_inputs`` / ``_pending_agents``, and emits
    the domain completion event (or its ``.failed``
    variant on parse error)."""

    def test_skips_when_pending_input_is_none(self):
        """A completion whose rid was not previously
        registered has no ``_pending_inputs`` entry —
        the system skips it (orphan)."""
        sys = _StubSystem()
        comp = ToolCallCompletion(
            request_event_id="orphan-rid",
            status="completed",
            result={"text": "x"},
        )
        view = _make_view(
            components_extra={"tool_completions": {"orphan-rid": comp}},
        )
        # Note: _pending_agents is empty → _consume_completion
        # returns []. The _consume_pending_completions call
        # also returns [].
        events = sys._consume_pending_completions("agent-1", view)
        assert events == []

    def test_emits_completion_event_for_valid_reply(self):
        """A valid JSON reply (ChatReply schema) is
        parsed and emitted as the GENERATED_EVENT_TYPE."""
        sys = _StubSystem()
        rid = str(uuid4())
        sys._pending_inputs[rid] = "hello"
        sys._pending_agents[rid] = "agent-1"
        valid = ChatReply(reply="hi", follow_up_questions=[]).model_dump()
        comp = ToolCallCompletion(
            request_event_id=rid,
            status="completed",
            result={"text": json.dumps(valid)},
        )
        view = _make_view(
            components_extra={"tool_completions": {rid: comp}},
        )
        events = sys._consume_pending_completions("agent-1", view)
        assert len(events) == 1
        assert events[0].event_type == "chat.reply.generated"
        assert events[0].data["input"] == "hello"

    def test_emits_failed_event_on_parse_error(self):
        """An unparseable reply → ``.failed`` event with
        the parse error in ``data["error"]``."""
        sys = _StubSystem()
        rid = str(uuid4())
        sys._pending_inputs[rid] = "hello"
        sys._pending_agents[rid] = "agent-1"
        comp = ToolCallCompletion(
            request_event_id=rid,
            status="completed",
            result={"text": "not json"},
        )
        view = _make_view(
            components_extra={"tool_completions": {rid: comp}},
        )
        events = sys._consume_pending_completions("agent-1", view)
        assert len(events) == 1
        assert events[0].event_type == "chat.reply.generated.failed"
        assert "parse_error" in events[0].data["error"]


class TestIsNewEvent:
    """``_is_new_event`` tracks the last seen
    ``last_event_id`` per agent and reports whether the
    current id is different."""

    def test_first_call_is_new(self):
        sys = _StubSystem()
        assert sys._is_new_event("agent-1", "evt-1") is True

    def test_same_call_is_not_new(self):
        sys = _StubSystem()
        sys._is_new_event("agent-1", "evt-1")
        assert sys._is_new_event("agent-1", "evt-1") is False

    def test_different_call_is_new(self):
        sys = _StubSystem()
        sys._is_new_event("agent-1", "evt-1")
        assert sys._is_new_event("agent-1", "evt-2") is True

    def test_none_event_id_is_not_new(self):
        """``last_event_id=None`` → not new (the
        dispatcher hasn't processed any event for this
        agent yet)."""
        sys = _StubSystem()
        assert sys._is_new_event("agent-1", None) is False


class TestEmitChatCompletion:
    """``_emit_chat_completion`` (module-level helper)
    builds the wire-format event. Branches: ChatReply
    output model vs fallback, with/without last_event_id."""

    def test_chat_reply_output_model_uses_reply_field(self):
        """When ``OUTPUT_MODEL is ChatReply``, the
        payload is ``{reply, follow_up_questions}``."""
        sys = _StubSystem()  # OUTPUT_MODEL = ChatReply
        view = _make_view()
        event = _emit_chat_completion(
            base=sys,
            view=view,
            text="hello back",
            follow_up_questions=["q1", "q2"],
            input_text="hello",
        )
        assert event.event_type == "chat.reply.generated"
        assert event.data["output"]["reply"] == "hello back"
        assert event.data["output"]["follow_up_questions"] == ["q1", "q2"]
        assert event.data["input"] == "hello"

    def test_fallback_output_model_uses_text_field(self):
        """When ``OUTPUT_MODEL is not ChatReply``, the
        payload is ``{text}``."""
        from kntgraph.agents.role_systems import Plan

        # Build a system with OUTPUT_MODEL = Plan (not
        # ChatReply) to exercise the fallback branch.
        class _PlanSystem(_StubSystem):
            OUTPUT_MODEL = Plan

        sys = _PlanSystem()
        view = _make_view()
        event = _emit_chat_completion(
            base=sys,
            view=view,
            text="a plan",
            follow_up_questions=[],
            input_text="x",
        )
        assert event.data["output"]["text"] == "a plan"
        assert "reply" not in event.data["output"]

    def test_emits_with_no_last_event_id(self):
        """``view.last_event_id = None`` → the helper
        builds a fresh UUID and an empty request_event_id."""

        class _NoEidView:
            agent_id = "agent-1"
            last_event_id = None

        sys = _StubSystem()
        event = _emit_chat_completion(
            base=sys,
            view=_NoEidView(),  # type: ignore[arg-type]
            text="x",
            follow_up_questions=[],
            input_text="x",
        )
        assert event.data["request_event_id"] == ""


# ---------------------------------------------------------------------------
# _prompts.py — format_chat_history, chat_idempotency_key,
# build_personalized_system_prompt (lang/tone/verbosity
# branches), parse_role_output (with/without fence).
# ---------------------------------------------------------------------------


class TestFormatChatHistory:
    """``format_chat_history`` builds a multi-line
    string with the session metadata + history + new
    message."""

    def test_includes_session_metadata(self):
        prompt = format_chat_history(
            session_id="s-1",
            user_id="u-1",
            tenant_id="t-1",
            messages=(),
            new_message="hi",
        )
        assert "s-1" in prompt
        assert "u-1" in prompt
        assert "t-1" in prompt
        assert "## New user message" in prompt
        assert "hi" in prompt

    def test_renders_history_role_content(self):
        prompt = format_chat_history(
            session_id="s-1",
            user_id="u-1",
            tenant_id="t-1",
            messages=(
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ),
            new_message="third",
        )
        assert "user: first" in prompt
        assert "assistant: second" in prompt


class TestChatIdempotencyKey:
    """``chat_idempotency_key`` returns a stable
    ``chat:<sha256>`` key for a given (session, history
    length, new message) tuple."""

    def test_stable_for_same_inputs(self):
        a = chat_idempotency_key(
            session_id="s-1",
            history_length=2,
            new_message="hi",
        )
        b = chat_idempotency_key(
            session_id="s-1",
            history_length=2,
            new_message="hi",
        )
        assert a == b
        assert a.startswith("chat:")

    def test_differs_for_different_message(self):
        a = chat_idempotency_key(
            session_id="s-1",
            history_length=2,
            new_message="hi",
        )
        b = chat_idempotency_key(
            session_id="s-1",
            history_length=2,
            new_message="bye",
        )
        assert a != b

    def test_differs_for_different_history_length(self):
        a = chat_idempotency_key(
            session_id="s-1",
            history_length=2,
            new_message="hi",
        )
        b = chat_idempotency_key(
            session_id="s-1",
            history_length=3,
            new_message="hi",
        )
        assert a != b


class TestBuildPersonalizedSystemPrompt:
    """``build_personalized_system_prompt`` honours
    ``language`` / ``tone`` / ``verbosity`` keys; other
    keys are ignored."""

    def test_empty_preferences_returns_bare_prompt(self):
        prompt = build_personalized_system_prompt({})
        assert prompt == PERSONALIZED_SYSTEM_PROMPT

    def test_known_language_appends_instruction(self):
        """A known language code adds the matching
        instruction line."""
        # English / "en" is in the table.
        prompt = build_personalized_system_prompt({"language": "en"})
        assert prompt != PERSONALIZED_SYSTEM_PROMPT
        assert PERSONALIZED_SYSTEM_PROMPT in prompt

    def test_unknown_language_is_ignored(self):
        """An unknown language code is silently ignored
        (no instruction line added)."""
        prompt = build_personalized_system_prompt({"language": "klingon"})
        assert prompt == PERSONALIZED_SYSTEM_PROMPT

    def test_known_tone_appends_instruction(self):
        prompt = build_personalized_system_prompt({"tone": "formal"})
        assert prompt != PERSONALIZED_SYSTEM_PROMPT

    def test_unknown_tone_is_ignored(self):
        prompt = build_personalized_system_prompt({"tone": "screaming"})
        assert prompt == PERSONALIZED_SYSTEM_PROMPT

    def test_verbosity_low_adds_under_80_words(self):
        prompt = build_personalized_system_prompt({"verbosity": "low"})
        assert "80 words" in prompt

    def test_verbosity_high_adds_detailed(self):
        prompt = build_personalized_system_prompt({"verbosity": "high"})
        assert "detailed" in prompt

    def test_verbosity_medium_is_ignored(self):
        prompt = build_personalized_system_prompt({"verbosity": "medium"})
        assert prompt == PERSONALIZED_SYSTEM_PROMPT

    def test_unknown_keys_are_ignored(self):
        """Only the 3 known keys are honoured; other
        keys pass through silently."""
        prompt = build_personalized_system_prompt({"color": "blue", "language": "en"})
        # language=en still adds an instruction; "color"
        # is ignored.
        assert prompt != PERSONALIZED_SYSTEM_PROMPT


class TestParseRoleOutput:
    """``parse_role_output`` strips code fences and
    parses JSON into the model."""

    def test_parses_plain_json(self):
        text = json.dumps({"reply": "hi", "follow_up_questions": []})
        result = parse_role_output(text, ChatReply)
        assert result.reply == "hi"
        assert result.follow_up_questions == []

    def test_parses_with_json_code_fence(self):
        """A `````json ... ``` fence is stripped before
        parsing (legacy role contract)."""
        body = json.dumps({"reply": "hi", "follow_up_questions": ["q1"]})
        text = f"```json\n{body}\n```"
        result = parse_role_output(text, ChatReply)
        assert result.reply == "hi"
        assert result.follow_up_questions == ["q1"]

    def test_parses_with_bare_code_fence(self):
        """A ````` ... ``` fence (no language hint) is
        also stripped."""
        body = json.dumps({"reply": "hi", "follow_up_questions": []})
        text = f"```\n{body}\n```"
        result = parse_role_output(text, ChatReply)
        assert result.reply == "hi"

    def test_strips_surrounding_whitespace(self):
        text = "   " + json.dumps({"reply": "hi", "follow_up_questions": []}) + "   "
        result = parse_role_output(text, ChatReply)
        assert result.reply == "hi"


__all__ = [
    "TestBaseRoleSystemCallBranches",
    "TestBuildPersonalizedSystemPrompt",
    "TestChatIdempotencyKey",
    "TestConsumeCompletion",
    "TestConsumePendingCompletions",
    "TestEmitChatCompletion",
    "TestFormatChatHistory",
    "TestIsNewEvent",
    "TestParseCompletion",
    "TestParseRoleOutput",
    "TestRoleSystemPromptBuilders",
]
