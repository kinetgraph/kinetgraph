# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``PrincipalLevel`` enum (ADR-060 §2.0).

The new enum is the canonical RBAC permission
(ADR-060 §2.0). The legacy :class:`Role` enum was
removed in v0.15; ``PrincipalLevel`` is the single
name going forward. The tests below assert:

  - The enum's values match the wire strings
    (``"service"``, ``"agent"``, ``"admin"``) so
    serialised principals round-trip.
  - Ordering (``__lt__`` / ``__le__``) follows the
    privilege order (``service < agent < admin``).
  - ``PrincipalLevel._coerce`` converts raw strings
    to enum values and raises ``ValueError`` on
    unknown strings (defensive deserialisation).
  - The enum's ``__lt__`` / ``__le__`` methods
    return ``NotImplemented`` for non-enum
    operands so the Python data model falls back
    to the right-hand side's comparator.
  - ``Principal.level`` is required (no default)
    and validates the admin-needs-no-tenant
    invariant.
  - ``Principal.is_admin`` reads ``level``
    directly.
"""

from __future__ import annotations

import pytest

from kntgraph.security import Principal, PrincipalLevel


class TestPrincipalLevelEnum:
    def test_values(self):
        """The enum's values match the wire strings."""
        assert PrincipalLevel.service.value == "service"
        assert PrincipalLevel.agent.value == "agent"
        assert PrincipalLevel.admin.value == "admin"

    def test_ordering(self):
        """Ordering: service < agent < admin."""
        assert PrincipalLevel.service < PrincipalLevel.agent
        assert PrincipalLevel.agent < PrincipalLevel.admin
        assert PrincipalLevel.service <= PrincipalLevel.agent
        assert PrincipalLevel.admin <= PrincipalLevel.admin

    def test_coerce_accepts_enum(self):
        """``_coerce(PrincipalLevel.X)`` returns X."""
        assert PrincipalLevel._coerce(PrincipalLevel.agent) is PrincipalLevel.agent

    def test_coerce_accepts_string(self):
        """``_coerce("agent")`` returns
        ``PrincipalLevel.agent``."""
        assert PrincipalLevel._coerce("admin") is PrincipalLevel.admin
        assert PrincipalLevel._coerce("service") is PrincipalLevel.service
        assert PrincipalLevel._coerce("agent") is PrincipalLevel.agent

    def test_coerce_rejects_unknown_string(self):
        """``_coerce("unknown")`` raises ``ValueError``
        so the caller learns of a corrupted serialised
        value instead of a silent fallback."""
        with pytest.raises(ValueError, match="Cannot convert"):
            PrincipalLevel._coerce("unknown")

    def test_lt_returns_not_implemented_for_non_enum(self):
        """``PrincipalLevel.X.__lt__("not-a-level")``
        returns ``NotImplemented`` (not raises
        ``TypeError``) so the Python data model can
        fall back to the right-hand side."""
        result = PrincipalLevel.admin.__lt__("not-a-level")
        assert result is NotImplemented

    def test_le_returns_not_implemented_for_non_enum(self):
        result = PrincipalLevel.admin.__le__("not-a-level")
        assert result is NotImplemented


class TestPrincipalLevelField:
    """The ``level`` field on :class:`Principal`."""

    def _make_principal(self, **kwargs):
        defaults = {
            "agent_id": "tenant-a.agent-1",
            "level": PrincipalLevel.agent,
            "tenant_id": "tenant-a",
            "key_id": "key-1",
        }
        defaults.update(kwargs)
        return Principal(**defaults)

    def test_default_level_is_agent(self):
        """A principal constructed with the helper's
        default has ``level=agent``.
        """
        p = self._make_principal()
        assert p.level == PrincipalLevel.agent

    def test_admin_level_requires_no_tenant(self):
        """The admin-needs-no-tenant invariant:
        ``level=admin`` with a non-null ``tenant_id``
        raises ``ValueError``."""
        with pytest.raises(ValueError, match="level=admin"):
            self._make_principal(
                level=PrincipalLevel.admin,
                tenant_id="tenant-A",
            )

    def test_non_admin_level_requires_tenant(self):
        """A principal with ``level=service`` and a
        null ``tenant_id`` raises ``ValueError``."""
        with pytest.raises(ValueError, match="level=service"):
            self._make_principal(
                level=PrincipalLevel.service,
                tenant_id=None,
            )

    def test_empty_tenant_id_rejected(self):
        """Empty ``tenant_id`` (not just None) is
        rejected for non-admin levels."""
        with pytest.raises(ValueError, match="non-empty tenant_id"):
            self._make_principal(
                level=PrincipalLevel.agent,
                tenant_id="",
            )

    def test_is_admin_reads_level(self):
        """``is_admin()`` returns True only for
        ``level=admin``."""
        admin = self._make_principal(
            level=PrincipalLevel.admin,
            tenant_id=None,
        )
        agent = self._make_principal(level=PrincipalLevel.agent)
        service = self._make_principal(level=PrincipalLevel.service)
        assert admin.is_admin() is True
        assert agent.is_admin() is False
        assert service.is_admin() is False
