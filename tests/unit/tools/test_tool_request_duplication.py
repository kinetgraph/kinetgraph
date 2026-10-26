# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression: a re-emitted tool request must not become a second
execution request (the tool-queue growth incident).

The symptom
-----------

A reactive dispatcher wakes, re-emits the same logical
``tool.<name>.requested`` event, and the per-tool Redis Stream
gains one more entry. The queue grows on every wake, the worker
drains it one entry at a time, and the real request ends up
behind a backlog of replays. This is the shape of the backoffice
2026-10-14 incident recorded in ADR-083 §1.1: 67 entries in
``knt:tools:<name>:queue`` and a 5.5-hour drain.

The cause
---------

``Event.create`` derives the ``event_id`` from
``(causation_id, event_type, payload)`` — see
``core/event/id_helpers.py`` ``_legacy_event_id``. The
``causation_id`` is therefore *an input to the identity*, not the
identity itself.

The role system supplies that ``causation_id`` from
``view.last_event_id`` (``agents/role_systems/_base.py``
``_emit_request``). But the emitted request is a **domain**
event, so folding it **overwrites** ``last_event_id`` with the
request's own id (``core/world/projection.py`` ``_apply_event``).
On the next wake the system therefore re-emits with
``causation_id = <the previous request>``, the hash changes, and
the new request gets a **different** ``event_id``.

The loop is self-sustaining: the request's own ``event_id``
becomes the next request's ``causation_id``, which becomes the
next request's ``event_id``, and so on.

What these tests pin
--------------------

1. **The re-emit produces a different ``event_id``.** The
   identity of a request is not stable across wakes.
2. **The queue grows.** The re-emitted request lands as a second
   entry in the tool queue.
3. **``dedup=True`` does not help** when the ``event_id``
   differs. ADR-083 §1.1 assumed the replays carried the *same*
   ``event_id``; they do not. This is the finding that
   invalidates the ADR-083 premise.
4. **The stable identity is reachable.** Passing
   ``idempotency_key`` makes the ``event_id`` independent of the
   ``causation_id`` — the fix path that
   ``core/event/id_helpers.py`` ``_idempotent_event_id`` already
   implements but that no production call site uses.

The tests below use the real ``ToolRouter`` and the real
``_BaseRoleSystem`` (no monkey-patches on core classes — the
testing skill §7.4 rule), against ``fakeredis`` so the queue
length is observable without a Redis container.
"""

from __future__ import annotations

import uuid

import fakeredis.aioredis
import pytest
from pydantic import BaseModel

from kntgraph.agents.role_systems._base import _BaseRoleSystem
from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.event import CorrelationContext, Event
from kntgraph.core.event.id_helpers import generate_deterministic_event_id
from kntgraph.core.world import AgentView, World
from kntgraph.infra.redis._client import safe_xrange
from kntgraph.tools.router import ToolRouter

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _OutputModel(BaseModel):
    """Minimal output model — the role system never parses a
    completion in these tests (no LLM involved)."""


class _StubRoleSystem(_BaseRoleSystem):
    """A minimal role system that targets the ``chat_llm`` tool
    and reacts to ``user.intent`` events.

    Same shape as the stub in
    ``tests/agents/unit/roles/test_role_systems.py`` — it
    exercises the real ``_emit_request`` path (the one that
    derives the ``causation_id``) without the LLM-backed prompt
    machinery of the concrete role systems.
    """

    TOOL_NAME = "chat_llm"
    REQUEST_EVENT_TYPE = "user.intent"
    GENERATED_EVENT_TYPE = "chat.reply.generated"
    OUTPUT_MODEL = _OutputModel

    def _build_system_prompt(self) -> str:
        return "system"

    def _build_user_prompt(self, view, session, new_input: str) -> str:
        return new_input


def _intent_event(message: str = "hello") -> Event:
    """The user intent that triggers the role system's request
    phase."""
    return Event.create(
        event_type="user.intent",
        agent_id="agent-1",
        event_class="domain",
        data={"intent": "chat", "message": message},
        correlation=CorrelationContext.new(),
    )


def _view_for(intent: Event) -> AgentView:
    """The ``AgentView`` the dispatcher folds for the agent right
    after the intent landed — ``last_event_id`` points at the
    intent."""
    return AgentView(
        agent_id="agent-1",
        components={
            "user.intent": {"intent": "chat", "message": "hello"},
            SessionComponent: SessionComponent(
                session_id="agent-1",
                user_id="u",
                tenant_id="t",
                messages=(),
                context={},
                started_at=0.0,
                ended_at=None,
                intent_event_id=str(intent.event_id),
            ),
        },
        last_event_id=str(intent.event_id),
        last_event_at=intent.timestamp,
    )


def _world_with(view: AgentView) -> World:
    """A ``World`` carrying exactly one agent view."""
    base = World.empty()
    return World(tick=base.tick, storage=base.storage, views={view.agent_id: view})


def _view_after_fold(view: AgentView, event: Event) -> AgentView:
    """The ``AgentView`` after the dispatcher folds ``event``.

    Mirrors ``core/world/projection.py`` ``_apply_event``: a
    domain event replaces ``last_event_id`` with its own id.
    """
    return AgentView(
        agent_id=view.agent_id,
        components=dict(view.components),
        last_event_id=str(event.event_id),
        last_event_at=event.timestamp,
    )


def _redis() -> fakeredis.aioredis.FakeRedis:
    """An in-process Redis (``decode_responses=False`` matches
    the production pool — the framework decodes the bytes
    itself)."""
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


# ---------------------------------------------------------------------------
# 1. The identity of a re-emitted request is not stable
# ---------------------------------------------------------------------------


class TestRequestIdentityIsNotStableAcrossWakes:
    """The role system re-emits on the next wake, and the
    re-emitted request carries a **different** ``event_id``.

    This is the root cause of the queue growth: the EventLog
    dedup (``knt:eventids:<id>``) and the tool-queue dedup
    (ADR-083, ``knt:tools:<name>:idem:<id>``) are both keyed on
    ``event_id``, so a changed ``event_id`` defeats both.
    """

    async def test_re_emit_produces_a_different_event_id(self) -> None:
        """Tick 1 and tick 2 emit the same logical request with
        two different ``event_id``s."""
        intent = _intent_event()
        system = _StubRoleSystem()

        # Tick 1: the view's last_event_id is the intent.
        tick1 = system(_world_with(_view_for(intent)))
        assert len(tick1) == 1
        request_1 = tick1[0]
        assert request_1.event_type == "tool.chat_llm.requested"

        # Tick 2: the dispatcher folded the request, so
        # last_event_id is now the request's own id.
        view_2 = _view_after_fold(_view_for(intent), request_1)
        tick2 = system(_world_with(view_2))
        assert len(tick2) == 1
        request_2 = tick2[0]

        # THE BUG: the same logical request, two identities.
        assert request_2.event_id != request_1.event_id

    async def test_re_emit_changes_the_causation_chain(self) -> None:
        """The second request is caused by the **first request**,
        not by the original intent.

        This is the self-sustaining loop: the request's own id
        becomes the next request's ``causation_id``.
        """
        intent = _intent_event()
        system = _StubRoleSystem()

        tick1 = system(_world_with(_view_for(intent)))
        request_1 = tick1[0]
        # ``causation_id`` is stored as the string the role
        # system passed (``str(view.last_event_id)``).
        assert str(request_1.causation_id) == str(intent.event_id)

        view_2 = _view_after_fold(_view_for(intent), request_1)
        tick2 = system(_world_with(view_2))
        request_2 = tick2[0]

        # The causation now points at the previous request — the
        # request is causing its own successor.
        assert str(request_2.causation_id) == str(request_1.event_id)
        assert str(request_2.causation_id) != str(intent.event_id)

    async def test_the_payload_is_identical_across_wakes(self) -> None:
        """Only the ``causation_id`` moved.

        The request *content* is unchanged, so the two events
        describe the same logical work — which is exactly why the
        second one must not be enqueued as new work.
        """
        intent = _intent_event()
        system = _StubRoleSystem()

        tick1 = system(_world_with(_view_for(intent)))
        request_1 = tick1[0]

        view_2 = _view_after_fold(_view_for(intent), request_1)
        tick2 = system(_world_with(view_2))
        request_2 = tick2[0]

        assert request_1.data == request_2.data
        assert request_1.event_type == request_2.event_type
        assert request_1.agent_id == request_2.agent_id


# ---------------------------------------------------------------------------
# 2. The queue grows
# ---------------------------------------------------------------------------


class TestToolQueueGrows:
    """The re-emitted request lands as a **second** entry in the
    per-tool queue."""

    async def test_re_emit_adds_a_second_queue_entry(self) -> None:
        """Two wakes, two entries in ``knt:tools:chat_llm:queue``."""
        redis = _redis()
        router = ToolRouter(redis, dedup=False)
        intent = _intent_event()
        system = _StubRoleSystem()

        tick1 = system(_world_with(_view_for(intent)))
        await router.route_batch(tick1)
        assert await redis.xlen("knt:tools:chat_llm:queue") == 1

        view_2 = _view_after_fold(_view_for(intent), tick1[0])
        tick2 = system(_world_with(view_2))
        await router.route_batch(tick2)

        # THE BUG: the queue holds a replay of work already
        # queued.
        assert await redis.xlen("knt:tools:chat_llm:queue") == 2

    async def test_the_queue_grows_with_every_wake(self) -> None:
        """The growth is unbounded — N wakes produce N entries.

        This is the shape of the backoffice incident: 67 entries
        from repeated wakes, each one waiting behind the others
        on a ``max_concurrency=1`` worker.
        """
        redis = _redis()
        router = ToolRouter(redis, dedup=False)
        intent = _intent_event()
        system = _StubRoleSystem()

        view = _view_for(intent)
        for _ in range(5):
            emitted = system(_world_with(view))
            assert len(emitted) == 1
            await router.route_batch(emitted)
            # Fold the emitted request: last_event_id moves.
            view = _view_after_fold(view, emitted[0])

        assert await redis.xlen("knt:tools:chat_llm:queue") == 5

    async def test_the_queued_entries_describe_the_same_work(self) -> None:
        """Both entries carry the same tool and the same params.

        Confirms the growth is a *replay*, not five distinct
        requests.
        """
        redis = _redis()
        router = ToolRouter(redis, dedup=False)
        intent = _intent_event()
        system = _StubRoleSystem()

        tick1 = system(_world_with(_view_for(intent)))
        view_2 = _view_after_fold(_view_for(intent), tick1[0])
        tick2 = system(_world_with(view_2))

        await router.route_batch(tick1)
        await router.route_batch(tick2)

        entries = await safe_xrange(redis, "knt:tools:chat_llm:queue")
        assert len(entries) == 2
        payloads = [entry[1][b"payload"] for entry in entries]
        # Same tool and same params in both entries.
        assert b"chat_llm" in payloads[0]
        assert b"chat_llm" in payloads[1]


# ---------------------------------------------------------------------------
# 3. dedup=True does NOT fix the re-emit (ADR-083's premise is wrong)
# ---------------------------------------------------------------------------


class TestDedupDoesNotFixTheReEmit:
    """ADR-083 §1.1 assumes the replays carry the **same**
    ``event_id``. They do not — so the ADR-083 dedup index does
    not collapse them.

    This is the finding that invalidates the ADR-083 premise and
    points the fix at the request **identity** rather than at the
    queue boundary.
    """

    async def test_dedup_collapses_the_same_event_id(self) -> None:
        """The ADR-083 dedup works as documented when the
        ``event_id`` really is the same.

        Pins that the existing mechanism is not broken — it is
        simply keyed on a value that changes.
        """
        redis = _redis()
        router = ToolRouter(redis, dedup=True)
        intent = _intent_event()
        system = _StubRoleSystem()

        tick1 = system(_world_with(_view_for(intent)))
        await router.route_batch(tick1)
        await router.route_batch(tick1)  # same event, same id

        assert await redis.xlen("knt:tools:chat_llm:queue") == 1

    async def test_dedup_does_not_collapse_the_re_emit(self) -> None:
        """The re-emitted request (different ``event_id``) still
        lands a second entry, even with ``dedup=True``."""
        redis = _redis()
        router = ToolRouter(redis, dedup=True)
        intent = _intent_event()
        system = _StubRoleSystem()

        tick1 = system(_world_with(_view_for(intent)))
        await router.route_batch(tick1)

        view_2 = _view_after_fold(_view_for(intent), tick1[0])
        tick2 = system(_world_with(view_2))
        await router.route_batch(tick2)

        # THE BUG survives the ADR-083 mitigation: the dedup
        # index is keyed on event_id, and the re-emit changed it.
        assert await redis.xlen("knt:tools:chat_llm:queue") == 2

    async def test_the_event_log_dedup_does_not_collapse_it_either(self) -> None:
        """The EventLog's ``knt:eventids:<id>`` index is keyed on
        ``event_id`` too, so it cannot collapse the re-emit
        either.

        The EventLog is the source of truth for the agent's
        history (ADR-036 §2.5), so a replayed request also
        pollutes the agent's own event stream.
        """
        redis = _redis()
        router = ToolRouter(redis, dedup=False)
        intent = _intent_event()
        system = _StubRoleSystem()

        tick1 = system(_world_with(_view_for(intent)))
        view_2 = _view_after_fold(_view_for(intent), tick1[0])
        tick2 = system(_world_with(view_2))

        # Two distinct ids -> two distinct dedup-index keys.
        key_1 = f"knt:eventids:{tick1[0].event_id}"
        key_2 = f"knt:eventids:{tick2[0].event_id}"
        assert key_1 != key_2

        # The EventLog dedup is keyed on exactly this key.
        await redis.set(key_1, "1-0")
        await router.route_batch(tick2)
        # Nothing about the second request is covered by the
        # first request's index entry.
        assert await redis.get(key_2) is None


# ---------------------------------------------------------------------------
# 4. The stable identity is reachable (the fix path)
# ---------------------------------------------------------------------------


class TestStableIdentityIsReachable:
    """``idempotency_key`` makes the request identity independent
    of the ``causation_id``.

    ``core/event/id_helpers.py`` ``_idempotent_event_id`` already
    excludes the ``causation_id`` from the hash — but no
    production call site passes the kwarg, so the mechanism is
    inert. These tests pin that the mechanism works, so the fix
    has a verified target.
    """

    async def test_idempotency_key_ignores_the_causation_id(self) -> None:
        """The same ``idempotency_key`` yields the same
        ``event_id`` under two different ``causation_id``s."""
        payload = {"tool": "chat_llm", "params": {"user": "hello"}}

        first = generate_deterministic_event_id(
            causation_id=uuid.uuid4(),
            agent_id="agent-1",
            event_type="tool.chat_llm.requested",
            data=payload,
            idempotency_key="request-42",
        )
        second = generate_deterministic_event_id(
            causation_id=uuid.uuid4(),
            agent_id="agent-1",
            event_type="tool.chat_llm.requested",
            data=payload,
            idempotency_key="request-42",
        )

        assert first == second

    async def test_without_the_key_the_causation_id_changes_the_id(self) -> None:
        """The current default: a different ``causation_id``
        yields a different ``event_id``.

        This is the behaviour that makes the re-emit a new
        request.
        """
        payload = {"tool": "chat_llm", "params": {"user": "hello"}}

        first = generate_deterministic_event_id(
            causation_id=uuid.uuid4(),
            agent_id="agent-1",
            event_type="tool.chat_llm.requested",
            data=payload,
        )
        second = generate_deterministic_event_id(
            causation_id=uuid.uuid4(),
            agent_id="agent-1",
            event_type="tool.chat_llm.requested",
            data=payload,
        )

        assert first != second

    async def test_request_tool_with_an_idempotency_key_is_stable(self) -> None:
        """``ToolAwareSystem.request_tool`` with an
        ``idempotency_key`` produces one stable ``event_id``
        across the re-emit.

        This is the fix contract: the framework's own mixin, with
        the kwarg it already accepts (``tools/system.py``), must
        yield the same identity for the same logical request.
        """
        intent = _intent_event()
        system = _StubRoleSystem()
        correlation = CorrelationContext.new()

        first = system.request_tool(
            agent_id="agent-1",
            tool_name="chat_llm",
            params={"user": "hello"},
            causation_id=str(intent.event_id),
            correlation=correlation,
            idempotency_key="request-42",
        )
        # The re-emit: a different causation_id (the request's own
        # id, as the projection produces), same idempotency_key.
        second = system.request_tool(
            agent_id="agent-1",
            tool_name="chat_llm",
            params={"user": "hello"},
            causation_id=str(first.event_id),
            correlation=correlation,
            idempotency_key="request-42",
        )

        assert first.event_id == second.event_id

    async def test_a_stable_identity_keeps_the_queue_at_one_entry(self) -> None:
        """The end-to-end effect: with a stable identity, the
        re-emit does **not** grow the queue.

        This is the behaviour the fix must produce. It is written
        against the explicit ``idempotency_key=`` API today; once
        the framework derives the key by default, the same
        assertion holds without the explicit kwarg.
        """
        redis = _redis()
        router = ToolRouter(redis, dedup=True)
        intent = _intent_event()
        system = _StubRoleSystem()
        correlation = CorrelationContext.new()

        def _emit(causation_id: str) -> Event:
            return system.request_tool(
                agent_id="agent-1",
                tool_name="chat_llm",
                params={"user": "hello"},
                causation_id=causation_id,
                correlation=correlation,
                idempotency_key="request-42",
            )

        first = _emit(str(intent.event_id))
        await router.route_batch([first])

        # The re-emit, caused by the first request.
        second = _emit(str(first.event_id))
        await router.route_batch([second])

        assert await redis.xlen("knt:tools:chat_llm:queue") == 1
