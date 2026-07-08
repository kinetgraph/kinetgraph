# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for `Principal.from_agent_id`.

The factory centralises the single-tenant derivation
convention (agent_id.partition(".")[0]) that was
previously open-coded in three call sites
(`kntgraph.api.auth`, `fmh_app.app._OpenVerifier`,
`fmh_office.mvp.http.StaticAPIKeyVerifier`).
"""

from __future__ import annotations

import pytest

from kntgraph.security import Principal, Role


class TestFromAgentId:
    """
    Derivation rules:
      - `tenant_id = agent_id.partition(".")[0]` if the
        agent_id has a separator.
      - `tenant_id = agent_id` for single-segment
        legacy form.
      - `role` is required (no default) — caller must
        be explicit.
      - `key_id` is required (no default) — the
        binding handle (revocation).
    """

    def test_dotted_agent_id_derives_tenant(self):
        p = Principal.from_agent_id("tenant-A.agent-1", role=Role.agent, key_id="k1")
        assert p.agent_id == "tenant-A.agent-1"
        assert p.tenant_id == "tenant-A"
        assert p.role == Role.agent
        assert p.key_id == "k1"

    def test_single_segment_agent_id_is_its_own_tenant(self):
        """`agent_id == tenant_id` is the legacy
        single-tenant form. We must NOT silently set
        `tenant_id=None` (admin-only) — the agent
        owns its own tenant."""
        p = Principal.from_agent_id("solo-agent", role=Role.agent, key_id="k1")
        assert p.agent_id == "solo-agent"
        assert p.tenant_id == "solo-agent"

    def test_admin_role_requires_tenant_id_none(self):
        """The factory passes through the role and
        tenant_id to the `__post_init__` invariant
        check. admin with a tenant raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            Principal.from_agent_id(
                "tenant-A.admin-1",
                role=Role.admin,
                key_id="k1",
            )
        assert "admin" in str(exc_info.value).lower()
        assert "tenant_id=None" in str(exc_info.value)

    def test_admin_with_no_separator_uses_whole_id_as_tenant(self):
        """Single-segment agent_id with role=admin
        would derive `tenant_id=agent_id` (not None),
        violating the admin invariant. The factory
        must raise so the caller picks a multi-tenant
        shape (or a different role)."""
        with pytest.raises(ValueError):
            Principal.from_agent_id(
                "lone-admin",
                role=Role.admin,
                key_id="k1",
            )

    def test_service_role_requires_non_empty_tenant(self):
        with pytest.raises(ValueError) as exc_info:
            Principal.from_agent_id(
                "",
                role=Role.service,
                key_id="k1",
            )
        # Either the `agent_id` check or the
        # `tenant_id` invariant — the order is
        # defined by `__post_init__`.
        assert "agent_id" in str(exc_info.value) or "tenant_id" in str(exc_info.value)

    def test_empty_agent_id_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            Principal.from_agent_id("", role=Role.agent, key_id="k1")
        assert "agent_id" in str(exc_info.value)

    def test_deeply_nested_uses_first_segment(self):
        """`a.b.c` -> tenant=`a`. The factory
        partitions on the FIRST separator, matching
        the legacy convention used by the Redis
        binding table."""
        p = Principal.from_agent_id("a.b.c", role=Role.agent, key_id="k1")
        assert p.tenant_id == "a"
        assert p.agent_id == "a.b.c"

    def test_result_is_principal(self):
        """The factory returns a real `Principal`
        (not a duck type); this keeps the type hint
        honest for callers."""
        p = Principal.from_agent_id("x.y", role=Role.agent, key_id="k1")
        assert isinstance(p, Principal)

    def test_owns_works_for_dotted(self):
        """`Principal.owns(agent_id)` must work for
        the dotted form (the most common case from
        the factory)."""
        p = Principal.from_agent_id("tenant-A.agent-1", role=Role.agent, key_id="k1")
        assert p.owns("tenant-A") is True
        assert p.owns("tenant-A.agent-1") is True
        assert p.owns("tenant-A.other") is True
        assert p.owns("tenant-B") is False
        assert p.owns("tenant-B.x") is False

    def test_owns_works_for_single_segment(self):
        """The legacy `agent_id == tenant_id` form
        must also work for `owns`."""
        p = Principal.from_agent_id("solo-agent", role=Role.agent, key_id="k1")
        assert p.owns("solo-agent") is True
        # `solo-agent.x` lives "under" the tenant.
        assert p.owns("solo-agent.x") is True
        assert p.owns("other") is False


class TestFromAgentIdRoundTrip:
    """
    `from_agent_id` produces the same `Principal` as
    a hand-written construction (so migration to the
    factory is mechanical and verified).
    """

    def test_matches_handwritten_construction(self):
        agent_id = "tenant-A.agent-1"
        p_factory = Principal.from_agent_id(agent_id, role=Role.agent, key_id="k1")
        p_hand = Principal(
            agent_id=agent_id,
            role=Role.agent,
            tenant_id="tenant-A",
            key_id="k1",
        )
        assert p_factory == p_hand

    def test_legacy_form_matches_handwritten(self):
        p_factory = Principal.from_agent_id("solo", role=Role.agent, key_id="k1")
        p_hand = Principal(
            agent_id="solo",
            role=Role.agent,
            tenant_id="solo",
            key_id="k1",
        )
        assert p_factory == p_hand
