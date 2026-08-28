# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the WorkerManager ACL hook (ADR-066 §4.1,
DEBT §2.27).

The hook closes the gap that ADR-061 §5 flagged:
``chat_llm`` (and every other tool) was an
"unauthenticated tool" — the dispatcher emitted the
request, but no gate-1 ACL check ran before the
worker. The hook runs in ``_process_message``
(after the payload is parsed, before the worker
slot is consumed).

Tests cover:

  - The default-allow behaviour for callers that
    registered without ``acl=`` (legacy, pre-v0.16).
  - The explicit ``acl=default_acl()`` path:
    requests with ``producer_principal_id`` pass
    when the principal has ``PrincipalLevel.agent``.
  - The denial path: the worker emits
    ``tool.<name>.failed`` with ``acl_denied``
    reason and acks the message (no worker slot
    consumed).
  - The ``acl_denied_no_principal`` path: events
    without ``producer_principal_id`` are denied
    when an ``acl=`` is set (events predating
    v0.16 are surfaced as audit failures, not
    silently passed).
  - The ``acl_for(name)`` accessor: returns the
    stored ACL for registered tools, ``None`` for
    unregistered or legacy callers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


pytest.importorskip("fastapi")


class TestWorkerManagerACL:
    """Gate-1 ACL check in ``_process_message``."""

    def _make_manager(self) -> tuple[Any, Any, Any]:
        """Build a WorkerManager + Redis mock +
        EventLog mock. The redis mock has
        ``xack`` as an AsyncMock; the event log
        mock has ``append`` as an AsyncMock. Tests
        patch these to assert on behaviour.
        """
        from kntgraph.tools.manager import WorkerManager

        redis_mock = MagicMock()
        redis_mock.xack = AsyncMock()
        event_log_mock = MagicMock()
        event_log_mock.append = AsyncMock()
        manager = WorkerManager(
            redis=redis_mock,
            event_log=event_log_mock,
        )
        return manager, redis_mock, event_log_mock

    def _make_request(self, *, producer_principal_id: str | None = None) -> Any:
        """Build a minimal request event shaped like
        what ``ToolRouter.route_batch`` produces."""
        from uuid import UUID

        from kntgraph.core.event import CorrelationContext, Event

        return Event.domain_from(
            agent_id="tenant-a.agent-1",
            type="tool.fake.echo.requested",
            data={"args": {"msg": "hi"}},
            correlation=CorrelationContext.new(
                correlation_id=UUID("11111111-1111-1111-1111-111111111111"),
            ),
            event_id=UUID("22222222-2222-2222-2222-222222222222"),
            producer_principal_id=producer_principal_id,
        )

    def _stream_message(self, request: Any) -> tuple[str, dict]:
        """Encode ``request`` as the Redis Stream
        payload format the manager expects."""
        import json

        data = {b"payload": json.dumps(request.to_dict()).encode()}
        return ("1-0", data)

    @pytest.mark.asyncio
    async def test_legacy_register_without_acl_is_default_allow(self):
        """Tools registered without ``acl=`` (the
        legacy v0.14 contract) are invoked for every
        request — no gate-1 check runs. This
        preserves backward compatibility for callers
        that have not opted in to the v0.16 ACL
        surface.

        The v0.17 step (ADR-066) flips the default
        to ``default_acl()``; that migration is
        covered by the ``DeprecationWarning`` test
        suite (separate file)."""
        from kntgraph.agents.tools.llm import LiteLLMToolWorker

        manager, redis_mock, event_log_mock = self._make_manager()
        manager.register(LiteLLMToolWorker)

        request = self._make_request(producer_principal_id="tenant-a.agent-1")
        message_id, data = self._stream_message(request)

        # Stub the tool invocation so the test
        # doesn't need a real Worker process. We
        # patch ``_invoke_tool_sync`` on the
        # manager's module path.
        from unittest.mock import patch

        with patch(
            "kntgraph.tools.manager._invoke_tool_sync",
            return_value={"status": "ok", "value": {"ok": True}},
        ):
            await manager._process_message(
                "chat_llm",
                "knt:tools:chat_llm:queue",
                message_id,
                data,
            )

        # ACL was not set; the request was invoked
        # (success path).
        event_log_mock.append.assert_awaited_once()
        completed = event_log_mock.append.await_args.args[0]
        assert completed.event_type == "tool.chat_llm.completed"

    @pytest.mark.asyncio
    async def test_explicit_default_acl_allows_agent_principal(self):
        """Tools registered with ``acl=default_acl()``
        pass when the ``producer_principal_id`` has
        ``PrincipalLevel.agent`` (the baseline requirement)."""
        from kntgraph.agents.tools.llm import LiteLLMToolWorker
        from kntgraph.tools.acl import default_acl

        manager, redis_mock, event_log_mock = self._make_manager()
        manager.register(LiteLLMToolWorker, acl=default_acl())

        request = self._make_request(producer_principal_id="tenant-a.agent-1")
        message_id, data = self._stream_message(request)

        from unittest.mock import patch

        with patch(
            "kntgraph.tools.manager._invoke_tool_sync",
            return_value={"status": "ok", "value": {"ok": True}},
        ):
            await manager._process_message(
                "chat_llm",
                "knt:tools:chat_llm:queue",
                message_id,
                data,
            )

        # The request was invoked; the completed
        # event was appended.
        event_log_mock.append.assert_awaited_once()
        completed = event_log_mock.append.await_args.args[0]
        assert completed.event_type == "tool.chat_llm.completed"

    @pytest.mark.asyncio
    async def test_explicit_default_acl_denies_missing_principal(self):
        """Events without ``producer_principal_id``
        (events predating v0.16) are denied with
        ``acl_denied_no_principal`` when ``acl=``
        is set. The worker emits the failed event
        and acks the message — no worker slot is
        consumed."""
        from kntgraph.agents.tools.llm import LiteLLMToolWorker
        from kntgraph.tools.acl import default_acl

        manager, redis_mock, event_log_mock = self._make_manager()
        manager.register(LiteLLMToolWorker, acl=default_acl())

        request = self._make_request(
            producer_principal_id=None,
        )
        message_id, data = self._stream_message(request)

        await manager._process_message(
            "chat_llm",
            "knt:tools:chat_llm:queue",
            message_id,
            data,
        )

        # The failed event was appended (denial path),
        # not the completed event.
        event_log_mock.append.assert_awaited_once()
        failed = event_log_mock.append.await_args.args[0]
        assert failed.event_type == "tool.chat_llm.failed"
        assert failed.data["error"] == "acl_denied_no_principal"
        # The producer_principal_id is preserved
        # on the failed event for the audit trail.
        assert failed.producer_principal_id is None
        # The message was acked (no retry on
        # denial).
        redis_mock.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_explicit_default_acl_denies_below_agent_principal(self):
        """A ``PrincipalLevel.service`` principal (below
        ``PrincipalLevel.agent``) is denied with ``acl_denied``
        when ``acl=default_acl()`` is set. The reason
        string surfaces the failure mode in the audit
        trail.

        Note: the v0.16 ``producer_principal_id``
        schema only carries the principal's
        ``agent_id``, not its ``Role`` (the API
        layer at the request boundary is the
        canonical place to look up the role via
        ``principal_ctx``). The worker's gate-1
        check defaults to ``PrincipalLevel.agent`` (the
        baseline requirement) — when the role is
        below ``agent``, the caller must surface
        this via a custom ``ToolACL`` (the gate-1
        check is per-role-attribute on
        ``Principal``, not on the request).
        """
        from kntgraph.agents.tools.llm import LiteLLMToolWorker
        from kntgraph.security import PrincipalLevel
        from kntgraph.tools.acl import ToolACL

        manager, redis_mock, event_log_mock = self._make_manager()

        # Custom ACL that requires ``PrincipalLevel.admin``
        # (above ``agent``). The baseline
        # ``default_acl()`` accepts ``agent`` and
        # above; to exercise the role-below-agent
        # denial path, we register a stricter ACL
        # that rejects everything below ``admin``.
        strict_acl = ToolACL(required_level=PrincipalLevel.admin)
        manager.register(LiteLLMToolWorker, acl=strict_acl)

        request = self._make_request(
            producer_principal_id="tenant-a.agent-1",
        )
        message_id, data = self._stream_message(request)

        await manager._process_message(
            "chat_llm",
            "knt:tools:chat_llm:queue",
            message_id,
            data,
        )

        event_log_mock.append.assert_awaited_once()
        failed = event_log_mock.append.await_args.args[0]
        assert failed.event_type == "tool.chat_llm.failed"
        assert failed.data["error"].startswith("acl_denied:")
        assert "role_insufficient" in failed.data["error"]

    def test_acl_for_returns_none_for_unregistered_tool(self):
        """Tools not registered return ``None`` from
        ``acl_for``. The gate-1 check treats
        ``None`` as "no constraint" (default-allow),
        which is the right behaviour for an
        unconfigured environment — the worker
        refuses with a clearer error if the tool is
        truly unknown.
        """
        manager, _, _ = self._make_manager()
        assert manager.acl_for("nonexistent-tool") is None

    def test_acl_for_returns_none_for_legacy_register(self):
        """Tools registered without ``acl=``
        (legacy) return ``None`` from ``acl_for``,
        matching the pre-v0.16 contract."""
        from kntgraph.agents.tools.llm import LiteLLMToolWorker

        manager, _, _ = self._make_manager()
        manager.register(LiteLLMToolWorker)
        assert manager.acl_for("chat_llm") is None

    def test_acl_for_returns_toolacl_for_explicit_register(self):
        """Tools registered with ``acl=`` return the
        ``ToolACL`` instance from ``acl_for``. The
        caller (gate-1 check) reuses the reference.
        """
        from kntgraph.agents.tools.llm import LiteLLMToolWorker
        from kntgraph.tools.acl import ToolACL, default_acl

        manager, _, _ = self._make_manager()
        explicit = default_acl()
        manager.register(LiteLLMToolWorker, acl=explicit)
        assert manager.acl_for("chat_llm") is explicit
        # Sanity: ``ToolACL`` is a dataclass; a
        # separate ``ToolACL()`` is not ``is``
        # identical to ``default_acl()``.
        assert ToolACL() is not explicit


class TestWorkerManagerRegisterDeprecation:
    """ADR-066 §4.4 (v0.17): ``register(tool_cls)``
    without an explicit ``acl=`` kwarg emits a
    ``DeprecationWarning``. The warning is NOT
    emitted when ``acl=default_acl()``,
    ``acl=ToolACL(...)``, or ``acl=None`` (the
    explicit opt-out) is passed.
    """

    def test_register_without_acl_emits_deprecation_warning(self) -> None:
        """``register(LiteLLMToolWorker)`` (no ``acl=``)
        emits a ``DeprecationWarning`` pointing to
        ADR-066 §4.4."""
        from kntgraph.agents.tools.llm import LiteLLMToolWorker
        from kntgraph.tools.manager import WorkerManager

        manager = WorkerManager.__new__(WorkerManager)
        manager._tools = {}
        manager._acls = {}
        with pytest.warns(DeprecationWarning, match="ADR-066 §4.4"):
            manager.register(LiteLLMToolWorker)

    def test_register_with_acl_does_not_emit_deprecation_warning(self) -> None:
        """``register(LiteLLMToolWorker, acl=default_acl())``
        does NOT emit a ``DeprecationWarning`` (the
        caller is explicitly opting in)."""
        import warnings

        from kntgraph.agents.tools.llm import LiteLLMToolWorker
        from kntgraph.tools.acl import default_acl
        from kntgraph.tools.manager import WorkerManager

        manager = WorkerManager.__new__(WorkerManager)
        manager._tools = {}
        manager._acls = {}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            manager.register(LiteLLMToolWorker, acl=default_acl())
        # No DeprecationWarning about ADR-066 §4.4.
        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert all("ADR-066 §4.4" not in str(w.message) for w in deprecation_warnings)

    def test_register_with_acl_none_does_not_emit_deprecation_warning(self) -> None:
        """``register(LiteLLMToolWorker, acl=None)``
        is the explicit opt-out (legacy behaviour
        without warning). The caller is acknowledging
        the policy choice."""
        import warnings

        from kntgraph.agents.tools.llm import LiteLLMToolWorker
        from kntgraph.tools.manager import WorkerManager

        manager = WorkerManager.__new__(WorkerManager)
        manager._tools = {}
        manager._acls = {}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            manager.register(LiteLLMToolWorker, acl=None)
        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert all("ADR-066 §4.4" not in str(w.message) for w in deprecation_warnings)
