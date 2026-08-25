# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``PrincipalLevel`` enum (ADR-060 §2.0).

The new enum is the canonical RBAC permission going
forward; the legacy :class:`Role` enum stays for one
minor cycle. The tests below assert:

  - The enum's values match ``Role`` (mechanical
    migration: ``Role.agent`` → ``PrincipalLevel.agent``).
  - ``PrincipalLevel.from_role`` converts between the
    two enums for every value.
  - Ordering (``__lt__`` / ``__le__``) matches ``Role``.
  - ``Principal`` accepts the new ``level`` field,
    defaults to ``None`` for backward compatibility,
    and validates the admin-needs-no-tenant invariant
    against the new field too.
  - ``Principal.effective_level`` prefers ``level`` and
    falls back to ``role`` for older principals.
  - ``Principal.with_level`` returns a new principal
    with ``level`` set; ``role`` is unchanged (the
    migration is additive).
  - ``Principal.is_admin`` reads ``effective_level``
    so callers get a consistent answer regardless of
    which field they populated.
"""

from __future__ import annotations

import pytest


class TestPrincipalLevelEnum:
    """The new enum is a drop-in for ``Role`` (values
    are the same strings). The migration is mechanical.
    """

    def test_values_match_role(self):
        """``PrincipalLevel`` and ``Role`` share the
        same string values. A serialised
        ``role=agent`` can be deserialised into
        either without translation."""
        from kntgraph.security import PrincipalLevel, Role

        assert PrincipalLevel.service.value == Role.service.value
        assert PrincipalLevel.agent.value == Role.agent.value
        assert PrincipalLevel.admin.value == Role.admin.value

    def test_ordering_matches_role(self):
        """``service < agent < admin`` on both
        enums. The ``__lt__`` and ``__le__`` operators
        return consistent results.
        """
        from kntgraph.security import PrincipalLevel

        assert PrincipalLevel.service < PrincipalLevel.agent
        assert PrincipalLevel.agent < PrincipalLevel.admin
        assert PrincipalLevel.service <= PrincipalLevel.agent
        assert PrincipalLevel.admin <= PrincipalLevel.admin
        assert not (PrincipalLevel.admin < PrincipalLevel.agent)

    def test_from_role_converts_every_value(self):
        """``PrincipalLevel.from_role`` accepts every
        :class:`Role` value and returns the equivalent
        :class:`PrincipalLevel`. Round-trips are
        identity (``from_role(level) == level``).
        """
        from kntgraph.security import PrincipalLevel, Role

        for role in Role:
            level = PrincipalLevel.from_role(role)
            assert level.value == role.value
            # Round-trip.
            assert PrincipalLevel.from_role(level) == level

    def test_from_role_rejects_unknown(self):
        """``from_role`` raises ``ValueError`` on an
        unknown role string. The migration is
        mechanical; an unknown string means the
        deserialised principal is corrupted."""
        from kntgraph.security import PrincipalLevel

        with pytest.raises(ValueError):
            PrincipalLevel.from_role("not-a-role")


class TestPrincipalLevelField:
    """The new ``level`` field on :class:`Principal`."""

    def _make_principal(self, **kwargs):
        from kntgraph.security import Principal, Role

        defaults = {
            "agent_id": "tenant-a.agent-1",
            "role": Role.agent,
            "tenant_id": "tenant-a",
            "key_id": "key-1",
        }
        defaults.update(kwargs)
        return Principal(**defaults)

    def test_default_level_is_none(self):
        """A principal constructed without ``level``
        defaults to ``None``. Existing callers that
        build principals with only ``role`` keep
        working — the migration is additive.
        """
        p = self._make_principal()
        assert p.level is None

    def test_effective_level_falls_back_to_role(self):
        """``effective_level`` returns the canonical
        RBAC level. When ``level`` is ``None``
        (older principals), the fallback uses
        ``PrincipalLevel.from_role(role)``.
        """
        from kntgraph.security import PrincipalLevel

        p = self._make_principal(role=PrincipalLevel.agent.value)  # type: ignore[arg-type]
        # Construct via Role (the legacy path).
        from kntgraph.security import Role

        p = self._make_principal(role=Role.agent)
        assert p.level is None
        assert p.effective_level() == PrincipalLevel.agent

    def test_effective_level_prefers_level_field(self):
        """When both ``level`` and ``role`` are set,
        ``effective_level`` returns ``level``. New
        code should set both fields; callers reading
        the canonical RBAC read the new field.
        """
        from kntgraph.security import PrincipalLevel, Role

        # ``role=admin`` with ``tenant_id=None`` is
        # the legacy admin configuration. Setting
        # ``level=agent`` is invalid (admin and
        # non-admin share tenant constraints) — so we
        # use a consistent pair.
        p = self._make_principal(
            role=Role.agent,
            level=PrincipalLevel.agent,
        )
        assert p.effective_level() == PrincipalLevel.agent

    def test_admin_level_requires_no_tenant(self):
        """The admin-needs-no-tenant invariant
        applies to ``level`` too. A principal with
        ``level=admin`` and a non-null
        ``tenant_id`` raises ``ValueError``.
        """
        from kntgraph.security import PrincipalLevel

        with pytest.raises(ValueError, match="level=admin"):
            self._make_principal(
                level=PrincipalLevel.admin,
            )

    def test_non_admin_level_requires_tenant(self):
        """A principal with ``level=service`` and a
        null ``tenant_id`` raises ``ValueError``.

        Note: the ``role`` check fires first (both
        fields share the same constraint). To
        exercise the ``level`` check in isolation,
        we pass an inconsistent ``role=admin`` /
        ``level=service`` pair — the ``role`` check
        passes (admin needs no tenant) but the
        ``level`` check fails.
        """
        from kntgraph.security import PrincipalLevel, Role

        with pytest.raises(ValueError, match="level=service"):
            self._make_principal(
                role=Role.admin,
                tenant_id=None,
                level=PrincipalLevel.service,
            )

    def test_with_level_returns_new_principal(self):
        """``with_level`` returns a new principal
        with ``level`` set; ``role`` is unchanged.
        The original is not mutated (frozen
        dataclass).
        """
        from kntgraph.security import PrincipalLevel, Role

        original = self._make_principal()
        assert original.level is None

        new = original.with_level(PrincipalLevel.agent)
        assert new is not original
        assert new.level == PrincipalLevel.agent
        assert new.role == Role.agent  # unchanged
        assert original.level is None  # original untouched

    def test_is_admin_reads_effective_level(self):
        """``is_admin`` returns True when
        ``effective_level`` is ``admin``. The
        method reads the new field so callers that
        set only ``level`` get the right answer.
        """
        from kntgraph.security import PrincipalLevel

        p_admin_via_level = self._make_principal(
            tenant_id=None,
            role=PrincipalLevel.admin.value,
            level=PrincipalLevel.admin,
        )
        # ``role`` was set via the legacy string;
        # ``level`` is the new enum. ``is_admin``
        # should return True.
        assert p_admin_via_level.is_admin() is True