# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.tools.registry -- framework-level ToolRegistry.

The registry holds the set of available tools for an
agent or application, with per-tool ACL. Lookup is by
``name``. The registry is intentionally simple (no
plugin discovery, no hot reload); applications that
need those can wrap this.

Each tool may carry an associated ``ToolACL`` (per
ADR-017 Scenario B). The default ACL is
``required_role=agent, tenant_pinned=False``;
callers can pass a stricter ACL via
``register_with_acl(tool, acl)`` or per-call via
``set_acl(name, acl)``. The ``acl_for(name)``
accessor is the single read path the framework
uses.

The ``list_descriptors`` method is used by the
``SolutionPromoter`` to populate ``(:Tool)`` nodes
in the Solution sub-graph of FalkorDB. The
serialisation logic (``_schema_to_json``) is
self-contained here so the registry remains the
canonical owner of the Tool shape.

Iter 25: moved from ``kntgraph.agents.tools.protocol`` to
the framework so that ``kntgraph`` modules
(``api.intent_router``) can depend on the canonical
home without leaking into the vertical package.
"""

from __future__ import annotations

import json
from typing import Mapping, Optional

import structlog

from .acl import ToolACL, default_acl
from .descriptors import ToolDescriptor
from .protocol import Tool, ToolArgValue


__all__ = ["ToolRegistry"]


class ToolRegistry:
    """
    Holds the set of available tools for an agent or
    application. Lookup is by ``name``.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._acls: dict[str, ToolACL] = {}

    def register(self, tool: Tool, *, acl: Optional[ToolACL] = None) -> None:
        """
        Register a tool with an optional ACL.

        When ``acl`` is None the default ACL is
        assigned (``required_role=agent, tenant_pinned=False``).
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        self._acls[tool.name] = acl if acl is not None else default_acl()

    def register_with_acl(self, tool: Tool, *, acl: ToolACL) -> None:
        """Convenience: register a tool with a custom ACL.
        Equivalent to ``register(tool, acl=acl)``.
        """
        self.register(tool, acl=acl)

    def set_acl(self, name: str, acl: ToolACL) -> None:
        """Replace the ACL for an already-registered tool.
        Idempotent w.r.t. the tool object — only the ACL
        changes. Raises ``KeyError`` if the tool is not
        registered.
        """
        if name not in self._tools:
            raise KeyError(f"Tool {name!r} not registered")
        self._acls[name] = acl

    def acl_for(self, name: str) -> Optional[ToolACL]:
        """Return the ACL for ``name`` (or None if the
        tool is not registered). The framework reads
        this at invoke time.
        """
        return self._acls.get(name)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._acls.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ------------------------------------------------------------------ introspection

    def list_descriptors(self) -> list[ToolDescriptor]:
        """
        Return a :class:`ToolDescriptor` for every
        registered tool.

        Used by the
        :class:`kntgraph.agents.memory.solutions.SolutionPromoter`
        to populate the ``(:Tool)`` nodes in the Solution
        sub-graph of FalkorDB. The promoter calls this
        on boot and ``MERGE``s each descriptor under the
        tenant's graph.
        """
        out: list[ToolDescriptor] = []
        for name in self.names():
            tool = self._tools[name]
            schema_json = _schema_to_json(tool.input_schema)
            if schema_json is None:
                continue
            out.append(
                ToolDescriptor(
                    name=tool.name,
                    description=tool.description,
                    input_schema_json=schema_json,
                )
            )
        return out


def _schema_to_json(schema: "Mapping[str, ToolArgValue]") -> "str | None":
    """
    Serialise a Tool's ``input_schema`` to a JSON string
    suitable for storage in FalkorDB.
    """
    log = structlog.get_logger()
    if schema is None:
        return "{}"
    try:
        serialised = json.dumps(schema, sort_keys=True, default=str)
    except (TypeError, ValueError) as e:
        log.warning(
            "tool_registry.schema_not_serialisable",
            error=str(e),
            schema_type=type(schema).__name__,
        )
        return None
    if "<" in serialised and "object at 0x" in serialised:
        log.warning(
            "tool_registry.schema_default_repr_used",
            schema_type=type(schema).__name__,
            note=(
                "Schema contained unrecognised types; "
                "json.dumps fell back to repr. Skipping "
                "this tool's descriptor."
            ),
        )
        return None
    try:
        json.loads(serialised)
    except (TypeError, ValueError) as e:
        log.warning(
            "tool_registry.schema_not_round_trippable",
            error=str(e),
            schema_type=type(schema).__name__,
        )
        return None
    return serialised
