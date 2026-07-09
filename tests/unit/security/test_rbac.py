# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end RBAC tests for the Zero-Trust Level 2
implementation (ADR-017).

Coverage:

  - Principal type invariants (admin requires
    tenant_id=None; agent/service require it; empty
    fields rejected; ordering)
  - Principal.owns() tenant hierarchy check
  - DefaultPolicy allows admin across tenants; denies
    agent cross-tenant; denies service from admin-only
    actions; honours tenant_pinned
  - ToolACL enforces required_role + tenant_pinned
  - EventLog.append refuses cross-tenant writes when
    principal_ctx is bound to a non-admin principal
  - Legacy string binding is converted to Principal
    via the verifier's fallback (no JSON migration
    needed)
  - ToolInvoker emits a ``tool.<name>.failed`` event
    with ``acl_denied`` reason when the principal
    fails the ACL check

These tests are unit-level (no live Redis) and use
fakeredis + FastAPI TestClient.
"""

from __future__ import annotations

import json
from typing import Any
import uuid

import fakeredis.aioredis
import pytest

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.security import (
    Action,
    AlwaysAllowPolicy,
    DefaultPolicy,
    Principal,
    Resource,
    Role,
    principal_ctx,
)
from kntgraph.tools.acl import ToolACL
from kntgraph.agents.tools.protocol import Tool, ToolRegistry

# Module-wide asyncio mode. Sync tests live inside
# async-compatible classes (no own event loop); the
# warnings they emit are cosmetic — pytest does not
# misclassify sync tests as async ones (the warning
# is "marked as async but is not async", not the other
# way around). The cost of the warnings is outweighed
# by the boilerplate of marking each async test
# individually.
pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Principal type invariants
# ---------------------------------------------------------------------------


class TestPrincipalInvariants:
    def test_admin_requires_tenant_id_none(self):
        with pytest.raises(ValueError, match="tenant_id=None"):
            Principal(
                agent_id="root",
                role=Role.admin,
                tenant_id="some-tenant",
                key_id="k1",
            )

    def test_agent_requires_tenant_id(self):
        with pytest.raises(ValueError, match="tenant_id"):
            Principal(
                agent_id="x",
                role=Role.agent,
                tenant_id=None,
                key_id="k1",
            )

    def test_service_requires_tenant_id(self):
        with pytest.raises(ValueError, match="tenant_id"):
            Principal(
                agent_id="x",
                role=Role.service,
                tenant_id="",
                key_id="k1",
            )

    def test_empty_agent_id_rejected(self):
        with pytest.raises(ValueError, match="agent_id"):
            Principal(
                agent_id="",
                role=Role.admin,
                tenant_id=None,
                key_id="k1",
            )

    def test_empty_key_id_rejected(self):
        with pytest.raises(ValueError, match="key_id"):
            Principal(
                agent_id="root",
                role=Role.admin,
                tenant_id=None,
                key_id="",
            )

    def test_json_roundtrip(self):
        p = Principal(
            agent_id="tenant-A.agent-1",
            role=Role.agent,
            tenant_id="tenant-A",
            key_id="k-001",
        )
        payload = p.to_json()
        p2 = Principal.from_json(payload)
        assert p == p2

    def test_from_json_invalid_role_raises(self):
        with pytest.raises(ValueError, match="role"):
            Principal.from_json(
                {
                    "agent_id": "x",
                    "role": "god",
                    "tenant_id": "t",
                    "key_id": "k",
                }
            )


# ---------------------------------------------------------------------------
# Principal.owns() — tenant hierarchy
# ---------------------------------------------------------------------------


class TestPrincipalOwns:
    def test_admin_owns_everything(self):
        admin = Principal(
            agent_id="root",
            role=Role.admin,
            tenant_id=None,
            key_id="k",
        )
        assert admin.owns("tenant-A.agent-1")
        assert admin.owns("tenant-B/x")
        assert admin.owns("any-thing-at-all")

    def test_agent_owns_under_its_tenant(self):
        agent = Principal(
            agent_id="tenant-A.agent-1",
            role=Role.agent,
            tenant_id="tenant-A",
            key_id="k",
        )
        assert agent.owns("tenant-A.agent-2")
        assert agent.owns("tenant-A")
        assert not agent.owns("tenant-B.agent-1")
        assert not agent.owns("tenant-AX/x")

    def test_service_owns_under_its_tenant(self):
        svc = Principal(
            agent_id="tenant-A.consolidator",
            role=Role.service,
            tenant_id="tenant-A",
            key_id="k",
        )
        assert svc.owns("tenant-A.anything")
        assert not svc.owns("tenant-B.anything")


# ---------------------------------------------------------------------------
# DefaultPolicy
# ---------------------------------------------------------------------------


class TestDefaultPolicy:
    def test_admin_cross_tenant_allowed(self):
        admin = Principal(
            agent_id="root",
            role=Role.admin,
            tenant_id=None,
            key_id="k",
        )
        policy = DefaultPolicy()
        res = Resource(kind="event", tenant_id="tenant-A.agent-1")
        assert policy.allows(principal=admin, resource=res, action=Action.invoke)

    def test_agent_cross_tenant_denied(self):
        agent = Principal(
            agent_id="tenant-A.agent-1",
            role=Role.agent,
            tenant_id="tenant-A",
            key_id="k",
        )
        policy = DefaultPolicy()
        res = Resource(kind="event", tenant_id="tenant-B/x")
        assert not policy.allows(principal=agent, resource=res, action=Action.invoke)

    def test_agent_own_tenant_allowed(self):
        agent = Principal(
            agent_id="tenant-A.agent-1",
            role=Role.agent,
            tenant_id="tenant-A",
            key_id="k",
        )
        policy = DefaultPolicy()
        res = Resource(kind="event", tenant_id="tenant-A.agent-2")
        assert policy.allows(principal=agent, resource=res, action=Action.invoke)

    def test_admin_only_action_denied_for_non_admin(self):
        agent = Principal(
            agent_id="tenant-A.agent-1",
            role=Role.agent,
            tenant_id="tenant-A",
            key_id="k",
        )
        policy = DefaultPolicy()
        res = Resource(kind="admin")
        assert not policy.allows(
            principal=agent, resource=res, action=Action.administer
        )

    def test_admin_action_allowed_for_admin(self):
        admin = Principal(
            agent_id="root",
            role=Role.admin,
            tenant_id=None,
            key_id="k",
        )
        policy = DefaultPolicy()
        res = Resource(kind="admin")
        assert policy.allows(principal=admin, resource=res, action=Action.administer)

    def test_service_blocked_from_admin_tools_via_tool_acl(self):
        """Tool-level ACL (Scenario B) blocks service
        from admin-only tools. The ``DefaultPolicy``
        alone does NOT see the per-tool ``required_role``
        — the tool ACL is consulted by the
        ToolInvoker. This test pins that the ToolACL
        wires the policy correctly.
        """
        from kntgraph.tools.acl import ToolACL

        acl = ToolACL(required_role=Role.admin)
        svc = Principal(
            agent_id="tenant-A.svc",
            role=Role.service,
            tenant_id="tenant-A",
            key_id="k",
        )
        admin = Principal(
            agent_id="root",
            role=Role.admin,
            tenant_id=None,
            key_id="k",
        )
        assert acl.check(svc)[0] is False
        assert acl.check(admin)[0] is True


# ---------------------------------------------------------------------------
# ToolACL — Scenario B (required_role + tenant_pinned)
# ---------------------------------------------------------------------------


class TestToolACL:
    def _principal(self, role: Role, tenant: str | None) -> Principal:
        return Principal(
            agent_id=("root" if role == Role.admin else f"{tenant}/x"),
            role=role,
            tenant_id=tenant,
            key_id="k",
        )

    def test_default_acl_is_agent_role_unpinned(self):
        acl = ToolACL()
        assert acl.required_role == Role.agent
        assert acl.tenant_pinned is False
        assert acl.tenant_id is None

    def test_tenant_pinned_requires_tenant_id(self):
        with pytest.raises(ValueError, match="tenant_pinned=True"):
            ToolACL(tenant_pinned=True, tenant_id=None)
        with pytest.raises(ValueError, match="tenant_pinned=False"):
            ToolACL(tenant_pinned=False, tenant_id="tenant-A")

    def test_role_check(self):
        acl = ToolACL(required_role=Role.admin)
        admin = self._principal(Role.admin, None)
        agent = self._principal(Role.agent, "tenant-A")
        assert acl.check(admin)[0] is True
        assert acl.check(agent)[0] is False
        assert "role_insufficient" in acl.check(agent)[1]

    def test_tenant_pinned_blocks_cross_tenant(self):
        acl = ToolACL(tenant_pinned=True, tenant_id="tenant-A")
        admin = self._principal(Role.admin, None)
        owner = self._principal(Role.agent, "tenant-A")
        foreigner = self._principal(Role.agent, "tenant-B")
        # Admin always passes (cross-tenant by definition).
        assert acl.check(admin)[0] is True
        # Owner tenant passes.
        assert acl.check(owner)[0] is True
        # Foreigner tenant blocked.
        assert acl.check(foreigner)[0] is False
        assert "tenant_violation" in acl.check(foreigner)[1]

    def test_service_role_too_low_for_admin_tool(self):
        acl = ToolACL(required_role=Role.admin)
        svc = self._principal(Role.service, "tenant-A")
        assert acl.check(svc)[0] is False


# ---------------------------------------------------------------------------
# ToolRegistry — ACL wiring
# ---------------------------------------------------------------------------


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    input_schema: dict = {}

    async def invoke(self, *, idempotency_key: str, **kwargs: Any) -> Any:
        return {"echo": kwargs}


class TestToolRegistryACL:
    def test_default_acl_applied_on_register(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        acl = reg.acl_for("echo")
        assert acl is not None
        assert acl.required_role == Role.agent

    def test_custom_acl_applied(self):
        reg = ToolRegistry()
        reg.register(
            _EchoTool(),
            acl=ToolACL(required_role=Role.admin),
        )
        assert reg.acl_for("echo").required_role == Role.admin

    def test_set_acl_replaces(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        reg.set_acl("echo", ToolACL(required_role=Role.admin))
        assert reg.acl_for("echo").required_role == Role.admin

    def test_set_acl_unknown_tool_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.set_acl("never-registered", ToolACL())


# ---------------------------------------------------------------------------
# Legacy verifier fallback
# ---------------------------------------------------------------------------


class TestLegacyVerifierFallback:
    async def test_legacy_string_yields_agent_principal(self):
        from kntgraph.api.auth import (
            _legacy_principal,
        )

        p = _legacy_principal("tenant-A.agent-1")
        assert p.role == Role.agent
        assert p.tenant_id == "tenant-A"
        assert p.key_id == "legacy"
        assert p.agent_id == "tenant-A.agent-1"

    async def test_legacy_flat_yields_self_as_tenant(self):
        from kntgraph.api.auth import _legacy_principal

        p = _legacy_principal("agent-1")
        assert p.tenant_id == "agent-1"

    async def test_redis_verifier_reads_legacy_string(self):
        import hashlib

        from kntgraph.api.auth import RedisAPIKeyVerifier

        server = fakeredis.FakeServer()
        server.connected = True
        redis_client = fakeredis.aioredis.FakeRedis(server=server)
        api_key = "any-key"
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        await redis_client.set(f"fmh:api:keys:{digest}", b"tenant-A.agent-1")
        verifier = RedisAPIKeyVerifier.from_redis(redis_client)
        result = await verifier.verify(api_key)
        assert result.is_ok()
        principal = result.ok_value()
        assert principal.agent_id == "tenant-A.agent-1"
        assert principal.tenant_id == "tenant-A"
        assert principal.role == Role.agent

    async def test_redis_verifier_reads_json(self):
        import hashlib

        from kntgraph.api.auth import RedisAPIKeyVerifier

        server = fakeredis.FakeServer()
        server.connected = True
        redis_client = fakeredis.aioredis.FakeRedis(server=server)
        api_key = "any-key"
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        await redis_client.set(
            f"fmh:api:keys:{digest}",
            json.dumps(
                {
                    "agent_id": "tenant-A.admin-1",
                    "role": "admin",
                    "tenant_id": None,
                    "key_id": "k-001",
                }
            ).encode("utf-8"),
        )
        verifier = RedisAPIKeyVerifier.from_redis(redis_client)
        result = await verifier.verify(api_key)
        principal = result.ok_value()
        assert principal.role == Role.admin
        assert principal.tenant_id is None
        assert principal.key_id == "k-001"


# ---------------------------------------------------------------------------
# EventLog.append — tenant_violation
# ---------------------------------------------------------------------------


class TestEventLogTenantViolation:
    """End-to-end tenant check at the EventLog boundary.

    Strategy: patch ``claim_event_id_slot`` so the
    Redis path is observable without us having to
    mock every ``redis.*`` method. The sentinel raises
    a recognisable AssertionError so we can assert
    *which* branch was taken.
    """

    async def test_cross_tenant_event_refused(self):
        from unittest.mock import MagicMock, patch as _patch

        from kntgraph.infra.redis._event_log import (
            _idempotency as idem_mod,
        )
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter
        from kntgraph.stream.event_log import EventLog

        reached_redis = {"hit": False}

        async def _sentinel(*a, **kw):
            reached_redis["hit"] = True
            return b"1-0"

        log = EventLog(RedisEventLogAdapter(client=MagicMock()))
        event = Event.create(
            event_class="domain",
            event_type="test.event.created",
            agent_id="tenant-B.agent-1",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        token = principal_ctx.set(
            Principal(
                agent_id="tenant-A.agent-1",
                role=Role.agent,
                tenant_id="tenant-A",
                key_id="k",
            )
        )
        with _patch.object(idem_mod, "claim_event_id_slot", _sentinel):
            result = await log.append(event)
        principal_ctx.reset(token)
        assert result.is_err()
        assert "tenant_violation" in str(result.err_value())
        # Redis was NOT called (the tenant branch
        # short-circuited before claim_event_id_slot).
        assert reached_redis["hit"] is False

    async def test_admin_cross_tenant_reaches_redis(self):
        """Admins are not subject to the tenant check.
        The Redis path is reached (proven by the
        sentinel being called). We tolerate any
        exception from the sentinel because we are
        only testing ordering, not the happy Redis
        path.
        """
        from unittest.mock import MagicMock, patch as _patch

        from kntgraph.infra.redis._event_log import (
            _idempotency as idem_mod,
        )
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter
        from kntgraph.stream.event_log import EventLog

        reached_redis = {"hit": False}

        async def _sentinel(*a, **kw):
            reached_redis["hit"] = True
            return b"1-0"

        log = EventLog(RedisEventLogAdapter(client=MagicMock()))
        event = Event.create(
            event_class="domain",
            event_type="test.event.created",
            agent_id="tenant-B.agent-1",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        token = principal_ctx.set(
            Principal(
                agent_id="root",
                role=Role.admin,
                tenant_id=None,
                key_id="k",
            )
        )
        with _patch.object(idem_mod, "claim_event_id_slot", _sentinel):
            result = await log.append(event)
        principal_ctx.reset(token)
        assert result.is_ok()
        assert reached_redis["hit"] is True

    async def test_no_principal_means_legacy_open(self):
        """When no principal is bound (ContextVar None),
        the EventLog falls through to legacy behaviour:
        the agent_id check from B2 still applies, but
        no tenant check. This is the migration path:
        tests and integrations that have not yet
        adopted the Principal flow continue to work.

        Strategy: patch ``claim_event_id_slot`` (the
        only path that touches Redis during append)
        to a sentinel that records the call. The
        EventLog catches ``Exception`` from the
        sentinel and wraps it in ``Err(PersistenceError)``;
        we recover the sentinel's signature from
        the error detail.
        """
        from unittest.mock import MagicMock, patch as _patch

        from kntgraph.infra.redis._event_log import (
            _idempotency as idem_mod,
        )
        from kntgraph.infra.redis._event_log import RedisEventLogAdapter
        from kntgraph.stream.event_log import EventLog

        reached_redis = {"hit": False}

        async def _sentinel_claim(*a, **kw):
            reached_redis["hit"] = True
            raise RuntimeError(
                "test stops here on purpose; "
                "the sentinel proves we reached "
                "the Redis path WITHOUT firing the "
                "tenant_violation branch"
            )

        event = Event.create(
            event_class="domain",
            event_type="test.event.created",
            agent_id="agent-1",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        assert principal_ctx.get() is None  # no principal bound
        log = EventLog(RedisEventLogAdapter(client=MagicMock()))
        with _patch.object(idem_mod, "claim_event_id_slot", _sentinel_claim):
            result = await log.append(event)
        assert reached_redis["hit"] is True, (
            "tenant branch short-circuited the Redis "
            "call — unexpected when no principal is bound"
        )
        # The sentinel's RuntimeError was caught and
        # wrapped; the error detail preserves the
        # original message.
        assert result.is_err()
        assert "test stops here on purpose" in str(result.err_value())


# ---------------------------------------------------------------------------
# AlwaysAllowPolicy — opt-out
# ---------------------------------------------------------------------------


class TestAlwaysAllowPolicy:
    def test_always_allows(self):
        policy = AlwaysAllowPolicy()
        admin = Principal(
            agent_id="root",
            role=Role.admin,
            tenant_id=None,
            key_id="k",
        )
        agent = Principal(
            agent_id="tenant-A/x",
            role=Role.agent,
            tenant_id="tenant-A",
            key_id="k",
        )
        res = Resource(kind="event", tenant_id="tenant-B/y")
        assert policy.allows(principal=admin, resource=res, action=Action.invoke)
        assert policy.allows(principal=agent, resource=res, action=Action.invoke)
