# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.components.role -- Role persona component (ADR-060 §3.0).

The :class:`RoleComponent` is the canonical ECS
representation of the agent's persona. It carries:

  - ``persona``: the role name (e.g. ``"chat"``,
    ``"service"``).
  - ``instructions``: the system prompt / persona
    instructions that drive the LLM-backed chat
    role systems.
  - ``allowed_tools``: the explicit allow-list of
    tool names the role can request. A role with
    ``allowed_tools=[]`` is forbidden to request any
    tool; the
    :meth:`_BaseRoleSystem._emit_request` gate
    short-circuits with an
    ``intent.validation_failed`` event when the
    role does not include the target tool.

**Why a Component, not raw dict.** Mirrors the
:class:`SessionComponent` / :class:`ProfileComponent`
pattern (ADR-042 §2.1): the role persona is a state
transition the system reads by class. The
``allowed_tools`` list is the role's policy; the
WorkerManager's per-tool ``ToolACL`` (ADR-066 §5
gate 1) is the principal's policy. The two
gates together form the
:ref:`Three-Gate Model <adr-060>`:

  1. **System-level** (gate 2): ``RoleComponent.allowed_tools``.
  2. **Worker-level** (gate 1): ``WorkerManager.acl_for(name).check(principal)``.
  3. **Tool-level** (the LLM worker itself): the worker's
     own ``invoke`` implementation (LLM workers do not
     consult ``ToolACL``; they execute the request).

A request that survives gate 2 still has to pass gate 1
(the principal). A request that survives both runs.

**Default behaviour.** The component is optional;
a system that does not install a ``RoleComponent``
on the view falls back to the legacy unconditional
emission (gate 2 not enforced). This is the
explicit opt-in pattern: deployments that want
strict role-based access control install the
component; the framework's default behaviour
preserves compatibility with deployments that
have not yet adopted role personas.

**Field drift is forbidden.** The component fields
mirror the equivalent fields in
``src/kntgraph/cli/templates/routing/components.py.jinja``.
Any change requires a corresponding change to the
template.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RoleComponent:
    """
    Semantic role for the agent (ADR-060 §3.0).

    The component is the canonical source of truth for
    the role persona's tool policy. The
    :class:`_BaseRoleSystem` reads
    ``view.components[RoleComponent]`` before
    emitting a tool request and short-circuits with
    ``intent.validation_failed`` when the target tool
    is not in :attr:`allowed_tools`.

    Args:
        persona: the role name (e.g. ``"chat"``,
            ``"service"``).
        instructions: the system prompt / persona
            instructions.
        allowed_tools: the explicit allow-list of
            tool names. Defaults to ``[]`` (the
            role is forbidden to request any tool
            when no allow-list is configured).
    """

    persona: str
    instructions: str
    allowed_tools: list[str] = field(default_factory=list)


def has_tool_access(role: RoleComponent | None, tool_name: str) -> bool:
    """Check whether the role permits the target tool.

    Returns ``True`` when ``role is None`` (the
    component is not installed; legacy behaviour
    is permissive). Returns ``True`` when
    ``tool_name`` is in ``role.allowed_tools``.
    Returns ``False`` otherwise (the role is
    installed but the tool is not in the
    allow-list).
    """
    if role is None:
        return True
    return tool_name in role.allowed_tools


__all__ = ["RoleComponent", "has_tool_access"]
