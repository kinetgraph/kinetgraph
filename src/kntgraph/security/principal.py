# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Zero-Trust Level 2 — Identity model (ADR-017).

The framework answers "who is calling?" via three types:

  Principal  — the immutable record of a caller.
  Role       — admin | agent | service.
  Action     — what the principal wants to do (used by
               the Policy evaluator).

This module is the source of truth for those types. The
HTTP gateway, the ToolInvoker, and the EventLog all
import Principal from here.

Storage
-------

A `Principal` is **not** serialised onto every event (that
would inflate every event with a metadata blob). Instead,
it is bound to the request coroutine via the
``principal_ctx`` ``ContextVar`` (this module) and read by
the `EventLog.append` boundary (per ADR-017 §3.3).

The binding table in Redis (per
``RedisAPIKeyVerifier``, ADR-012) stores the Principal
as JSON under the API-key hash.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, TypeVar, runtime_checkable

from kntgraph.core._typing import JsonValue


# Generic comparable type for ``Role.__lt__`` / ``Role.__le__``.
# The Python data model requires ``__lt__(self, other) -> bool``
# to accept any value (the caller may compare against a
# non-Role); the runtime check is ``isinstance(other, Role)``.
# The TypeVar avoids leaking ``object`` into the framework
# surface while keeping the signature compatible with the
# ``SupportsRichComparison`` protocol.
ComparableT = TypeVar("ComparableT")


class Role(str, Enum):
    """
    Roles a Principal may assume.

    Ordering (lower < higher privilege):
      service < agent < admin

    Used by `Policy.allows(principal, resource, action)`
    to gate operations at the framework boundary.
    """

    service = "service"  # background workers; tenant-scoped
    agent = "agent"  # user-facing agents; tenant-scoped
    admin = "admin"  # cross-tenant operators; tenant=None

    def __lt__(self, other: ComparableT) -> bool:
        order = (Role.service, Role.agent, Role.admin)
        # ``other`` may be a Role or any comparable value;
        # equality short-circuits before the index lookup.
        if not isinstance(other, Role):
            return NotImplemented
        return order.index(self) < order.index(other)

    def __le__(self, other: ComparableT) -> bool:
        if not isinstance(other, Role):
            return NotImplemented
        return self == other or self < other


@dataclass(frozen=True, slots=True)
class Principal:
    """
    Immutable identity record.

    Attributes
    ----------
    agent_id : str
        The producer identity (e.g. ``"tenant-A.agent-1"``).
        Conventionally starts with ``tenant_id + "/"`` for
        non-admin principals; the framework enforces this
        at the `EventLog.append` boundary (see
        ``EventLog._validate_principal_tenant``).
    role : Role
        One of ``admin``, ``agent``, ``service``.
    tenant_id : Optional[str]
        Tenant scope. Must be non-null for ``agent`` and
        ``service``; must be null for ``admin``. The
        constructor enforces this invariant.
    key_id : str
        The API-key identifier. Used for revocation: a
        ``delete`` on the binding table removes the
        principal entirely.

    Invariants
    ----------
    The constructor raises ``ValueError`` on:
      - ``role=admin`` with a non-null ``tenant_id``
      - ``role in (agent, service)`` with a null
        ``tenant_id``
      - empty ``tenant_id`` (None is fine for admin only)
    """

    agent_id: str
    role: Role
    tenant_id: Optional[str]
    key_id: str

    def __post_init__(self) -> None:
        if not self.agent_id:
            raise ValueError("Principal.agent_id must be non-empty")
        if not self.key_id:
            raise ValueError("Principal.key_id must be non-empty")
        if self.role == Role.admin:
            if self.tenant_id is not None:
                raise ValueError(
                    f"Principal.role=admin requires "
                    f"tenant_id=None; got {self.tenant_id!r}"
                )
        else:
            if self.tenant_id is None or not self.tenant_id:
                raise ValueError(
                    f"Principal.role={self.role.value} requires a non-empty tenant_id"
                )

    def is_admin(self) -> bool:
        """True when this principal is cross-tenant."""
        return self.role == Role.admin

    def owns(self, agent_id: str) -> bool:
        """
        Whether this principal may act on behalf of
        ``agent_id``.

        Admin owns everything. Non-admin owns only
        ``agent_id`` that lives under its tenant.
        The separator is ``.`` (single dot) — chosen
        over ``/`` to remain compatible with the
        ``agent_id`` character class
        (``[A-Za-z0-9._:-]{1,128}``) enforced by the
        EventLog trust boundary (see B2 / ADR-017
        §2.2 footnote). Tenant examples:
          - ``tenant-A.agent-1`` → tenant ``tenant-A``
          - ``tenant-A`` (no separator) → tenant
            ``tenant-A`` (single-segment legacy).
        """
        if self.is_admin():
            return True
        if self.tenant_id is None:
            return False  # unreachable: enforced by __post_init__
        return agent_id == self.tenant_id or agent_id.startswith(self.tenant_id + ".")

    def to_json(self) -> dict[str, JsonValue]:
        """Serialise to the wire format stored in Redis."""
        return {
            "agent_id": self.agent_id,
            "role": self.role.value,
            "tenant_id": self.tenant_id,
            "key_id": self.key_id,
        }

    @classmethod
    def from_json(cls, payload: dict[str, JsonValue]) -> "Principal":
        """Parse the wire format. Raises ``ValueError`` on
        invalid input (including legacy string-only payloads
        — see ``scripts/migrate_principals.py``).
        """
        if not isinstance(payload, dict):
            raise ValueError(
                f"Principal JSON must be a dict, got {type(payload).__name__}"
            )
        try:
            role = Role(payload["role"])
        except (KeyError, ValueError) as e:
            raise ValueError(f"Principal.role missing or invalid: {e}") from e
        return cls(
            agent_id=_scalar(payload.get("agent_id")),
            role=role,
            tenant_id=_optional_scalar(payload.get("tenant_id")),
            key_id=_scalar(payload.get("key_id")),
        )

    @classmethod
    def from_agent_id(
        cls,
        agent_id: str,
        *,
        role: Role,
        key_id: str,
    ) -> "Principal":
        """
        Build a `Principal` from an `agent_id` using the
        single-tenant derivation convention:

          - `tenant_id = agent_id.partition(".")[0]` if a
            separator is present.
          - else `tenant_id = agent_id` (single-segment
            legacy form).

        The convention is repeated in three call sites
        (`kntgraph.api.auth.RedisAPIKeyVerifier`,
        `fmh_app.app._OpenVerifier`,
        `fmh_office.mvp.http.StaticAPIKeyVerifier`) —
        this factory is the single source of truth.

        `role` is required (no default) because the
        caller must be explicit about the privilege
        level being granted. `key_id` identifies the
        binding (revocation handle); use a stable
        string per verifier (`"legacy"`, `"dev-open"`,
        `"demo"`, etc.).
        """
        if not agent_id:
            raise ValueError("agent_id is empty")
        tenant_id = agent_id.partition(".")[0] or agent_id
        return cls(
            agent_id=agent_id,
            role=role,
            tenant_id=tenant_id,
            key_id=key_id,
        )


def _scalar(value: JsonValue) -> str:
    """Coerce a ``JsonValue`` slot to ``str``.

    The ``Principal`` fields are all ``str`` in the
    dataclass but the wire format (and the EventLog
    payload) carries them as ``JsonValue`` to match the
    storage contract. Returns ``""`` for None or
    non-scalar shapes (defence-in-depth).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _optional_scalar(value: JsonValue) -> Optional[str]:
    """Like :func:`_scalar` but propagates ``None`` as
    ``None`` (the ``tenant_id`` field is optional).
    """
    if value is None:
        return None
    return _scalar(value)


# ---------------------------------------------------------------------------
# Action / Resource — the policy contract (ADR-017 §2.1)
# ---------------------------------------------------------------------------


class Action(str, Enum):
    """What a principal wants to do."""

    read = "read"  # read state (events, tools, agents)
    write = "write"  # write state (append events)
    invoke = "invoke"  # invoke a tool
    administer = "administer"  # admin operations (cross-tenant)


@dataclass(frozen=True, slots=True)
class Resource:
    """The object of an action. ``kind`` discriminates."""

    kind: str  # "event" | "tool" | "agent" | "tenant" | "admin"
    tenant_id: Optional[str] = None
    name: Optional[str] = None


@runtime_checkable
class Policy(Protocol):
    """
    The framework's authorization contract.

    Implementations:
      - ``AlwaysAllowPolicy`` — no-op (legacy mode)
      - ``DefaultPolicy`` — role + tenant (zero-trust mode)

    Policies are stateless; the framework constructs one at
    boot and reuses it. Mutations (e.g. role-grants) require
    constructing a new ``Policy`` and swapping the binding
    via ``set_policy(...)``.
    """

    def allows(
        self,
        *,
        principal: Principal,
        resource: Resource,
        action: Action,
    ) -> bool: ...


class AlwaysAllowPolicy:
    """
    No-op policy. Every principal may do everything.

    Used during the migration window before
    Zero-Trust is fully wired (and during unit tests
    that exercise non-RBAC behaviour).
    """

    def allows(
        self,
        *,
        principal: Principal,
        resource: Resource,
        action: Action,
    ) -> bool:
        return True


class DefaultPolicy:
    """
    Zero-Trust Level 2 policy.

    Rules:
      - ``admin``: every action is allowed (cross-tenant).
      - ``service`` and ``agent``: the principal must
        "own" the resource's tenant (i.e.
        ``principal.tenant_id == resource.tenant_id`` or
        ``resource.tenant_id`` is a sub-path of
        ``principal.tenant_id``). For ``action=administer``
        a non-admin is always denied.
      - The ``invoke`` action additionally requires
        ``principal.role >= resource.min_role`` (set by
        ``ToolDescriptor.required_role``). The default
        (``min_role=agent``) means services and agents
        may invoke the tool; admin tools require the
        principal to be an admin.

    Tenant comparison accepts the legacy convention where
    ``agent_id == tenant_id`` (no slash separator) — see
    ``Principal.owns``.
    """

    def allows(
        self,
        *,
        principal: Principal,
        resource: Resource,
        action: Action,
    ) -> bool:
        if principal.role == Role.admin:
            return True
        if action == Action.administer:
            return False  # admin-only action
        # Tenant ownership check.
        if resource.tenant_id is None:
            # Resource has no tenant — treat as
            # cross-tenant (admin-only).
            return False
        if principal.tenant_id is None:
            return False  # unreachable per Principal.__post_init__
        if not principal.owns(resource.tenant_id):
            return False
        # Role-level check for tool invocation.
        if action == Action.invoke and resource.kind == "tool":
            min_role = getattr(resource, "min_role", Role.agent)
            if principal.role < min_role:
                return False
        return True


# ---------------------------------------------------------------------------
# ContextVar — per-request principal binding (ADR-017 §3.3)
# ---------------------------------------------------------------------------


#: ``ContextVar[Principal | None]`` bound by the request
#: middleware. ``EventLog.append`` and ``ToolInvoker``
#: read this — never write. The ``None`` default
#: indicates "no principal bound", which means the
#: caller's intent cannot be authorised and any
#: guarded operation must raise.
principal_ctx: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar(
    "fmh_principal", default=None
)


__all__ = [
    "Action",
    "AlwaysAllowPolicy",
    "DefaultPolicy",
    "Policy",
    "Principal",
    "Resource",
    "Role",
    "principal_ctx",
]
