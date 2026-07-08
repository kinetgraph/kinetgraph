# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Re-export of the framework-level ``kntgraph.tools.protocol``
module (Iter 25).

The canonical home of the Tool Protocols is
``kntgraph.tools.protocol``. This module re-exports
the three layered Protocols (``Describable``,
``Callable``, ``Tool``), the value objects
(``ToolEventType``, ``ToolCall``, ``ToolArgValue``),
the registry (``ToolRegistry``), the descriptor
(``ToolDescriptor``), and the ACL helpers
(``ToolACL``, ``default_acl``) for backward
compatibility.

The vertical concrete tools (``PiiRedactionTool``,
``LiteLLMTool``, ...) live in this package and
implement the canonical ``Tool`` Protocol from
``kntgraph.tools.protocol``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from kntgraph.tools.acl import ToolACL as ToolACL
from kntgraph.tools.acl import default_acl as default_acl
from kntgraph.tools.descriptors import ToolDescriptor as ToolDescriptor
from kntgraph.tools.protocol import Callable as Callable
from kntgraph.tools.protocol import Describable as Describable
from kntgraph.tools.protocol import Tool as Tool
from kntgraph.tools.protocol import ToolArgValue as ToolArgValue
from kntgraph.tools.registry import ToolRegistry as ToolRegistry


__all__ = [
    "Callable",
    "Describable",
    "Tool",
    "ToolACL",
    "ToolArgValue",
    "ToolCall",
    "ToolDescriptor",
    "ToolEventType",
    "ToolRegistry",
    "default_acl",
]


# ``R`` and ``P`` are kept as the legacy type variables
# so existing imports (`from kntgraph.agents.tools.protocol
# import R, P`) continue to work.
from kntgraph.tools.protocol import R, P  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Request / Response event types (legacy, vertical-specific)
# ---------------------------------------------------------------------------


class ToolEventType:
    """
    Event types for tool invocation, used by the
    `invoke_via_event_log` helper.

    Convention:
      - "tool.<name>.requested" : a system asks for the tool
      - "tool.<name>.completed"  : the tool returned Ok(...)
      - "tool.<name>.failed"     : the tool returned Err(...)
      - "tool.<name>.args_invalid" : the request was rejected
        before the Tool was invoked because the merged
        args (caller's + semantic extraction) did not
        validate against the Tool's `input_schema`. The
        event is consumed by the DLQ / replay path
        (ADR-013 §2.2).
    """

    @staticmethod
    def requested(tool_name: str) -> str:
        return f"tool.{tool_name}.requested"

    @staticmethod
    def completed(tool_name: str) -> str:
        return f"tool.{tool_name}.completed"

    @staticmethod
    def failed(tool_name: str) -> str:
        return f"tool.{tool_name}.failed"

    @staticmethod
    def args_invalid(tool_name: str) -> str:
        return f"tool.{tool_name}.args_invalid"


# ---------------------------------------------------------------------------
# In-memory tool result (non-streaming)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCall:
    """
    A single call to a tool. The `result` is `None` until
    the tool completes. The `tool_call_id` is the
    `event_id` of the `.requested` event — used for
    idempotency.
    """

    tool_call_id: str
    tool_name: str
    agent_id: str
    arguments: "Mapping[str, ToolArgValue]"
    result: "Optional[ToolArgValue]" = None
    error: Optional[str] = None
    completed: bool = False
    latency_ms: Optional[float] = None
