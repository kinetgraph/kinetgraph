# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tool-level authorisation (ADR-017 §5, Scenario B).

Each registered tool can carry an optional
``ToolACL`` describing:

  - ``required_role``: minimum role the caller must
    have. Defaults to ``Role.agent`` (the most common
    case for user-facing tools).

  - ``tenant_pinned``: when True, the tool may only be
    invoked by principals whose ``tenant_id`` matches
    the tool's ``tenant_id``. The ``tenant_id`` field
    is set at registration time; tools registered as
    pinned to ``tenant-A`` are invisible to principals
    of ``tenant-B`` even if they pass the role check.

The framework's contract is that ACL is enforced
**at the ToolInvoker**, not at registration time. The
``ToolRegistry`` stores ACL alongside the tool; the
invoker calls ``ToolACL.check(principal)`` before
dispatch.

Iter 25: moved from ``kntgraph.agents.tools.acl`` to the
framework so that ``kntgraph.modules`` can depend
on the canonical home without leaking into the
vertical package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from kntgraph.security import Principal, Role


__all__ = ["ToolACL", "default_acl"]


@dataclass(frozen=True, slots=True)
class ToolACL:
    """
    Authorisation metadata for a registered tool
    (ADR-017 Scenario B).

    Attributes
    ----------
    required_role : Role
        The minimum role a principal must hold to
        invoke this tool. Defaults to ``Role.agent``.
    tenant_pinned : bool
        When True, only principals whose
        ``tenant_id == tenant_id`` (or admins) may
        invoke the tool. Defaults to False.
    tenant_id : Optional[str]
        The owning tenant. Required when
        ``tenant_pinned=True``; must be None when
        ``tenant_pinned=False`` (a pinned tool has a
        tenant; an unpinned tool is global within its
        required_role).
    """

    required_role: Role = Role.agent
    tenant_pinned: bool = False
    tenant_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.tenant_pinned:
            if not self.tenant_id:
                raise ValueError(
                    "ToolACL.tenant_pinned=True requires a non-empty tenant_id"
                )
        else:
            if self.tenant_id is not None:
                raise ValueError("ToolACL.tenant_pinned=False requires tenant_id=None")

    def check(self, principal: Principal) -> tuple[bool, str]:
        """
        Evaluate whether ``principal`` may invoke a
        tool with this ACL. Returns ``(allowed, reason)``
        where ``reason`` is an empty string on success
        and a short string explaining the failure on
        refusal. The caller surfaces the reason in
        ``ToolError`` and the audit log.

        The check is deliberately conservative: any
        uncertainty results in denial. Order of checks
        (cheapest first):

          1. role match (cheap O(1) on enum)
          2. tenant match (cheap string compare)
        """
        # 1. Role check.
        if principal.role < self.required_role:
            return (
                False,
                f"role_insufficient: "
                f"required {self.required_role.value}, "
                f"got {principal.role.value}",
            )
        # 2. Tenant check (only when pinned).
        if self.tenant_pinned:
            if principal.is_admin():
                return (True, "")
            if principal.tenant_id != self.tenant_id:
                return (
                    False,
                    f"tenant_violation: "
                    f"tool pinned to {self.tenant_id!r}, "
                    f"principal tenant {principal.tenant_id!r}",
                )
        return (True, "")


def default_acl() -> ToolACL:
    """Unpinned, agent-role. Equivalent to the framework's
    behaviour before ADR-017.
    """
    return ToolACL()
