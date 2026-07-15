# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.tools — Tool subsystem (F8.2).

Vertical-owned: how agents invoke external capabilities
(fiscal authority, ERP, bank, etc). The Tool Protocol,
the Registry, the ACL, and the bundled PII / LLM
tools all live here.

The framework (kntgraph) owns the **primitives**
(``kntgraph.tools.protocol``, ``acl.py``,
``descriptors.py``, ``registry.py``, ``schema.py``).
This module re-exports them so existing imports
(``from kntgraph.agents.tools import Tool, ToolRegistry,
ToolEventType``) keep working. New code should prefer
the framework path.

Module layout
-------------

Framework-level types (in ``kntgraph.tools``)

* ``protocol`` -- :class:`Describable`, :class:`Callable`,
  :class:`Tool` (the three layered Protocols).
* ``registry`` -- :class:`ToolRegistry` (per-process
  registry with ACL).
* ``acl`` -- :class:`ToolACL`, :func:`default_acl`
  (per-tool authorisation; ADR-017 §5).
* ``descriptors`` -- :class:`ToolDescriptor` (the
  canonical descriptor dataclass).
* ``schema`` -- :class:`FieldSpec`, :func:`walk_schema`,
  :func:`compute_schema_version` (JSON-Schema view).

Vertical-owned (this package)

* ``arg_validation`` -- light JSON-Schema validation
  for tool kwargs (consumes the framework's
  ``walk_schema``).
* ``capability`` -- :class:`Capability` (semantic alias
  for Tool; ADR-006).
* ``llm_transport`` -- :class:`LLMTransport` Protocol
  (generic LLM I/O boundary).
* ``pii`` -- :class:`PiiRedactionTool` (bundled PII
  redaction tool).

Concrete implementations

* ``llm`` -- :class:`LiteLLMToolWorker` (the canonical
  LLM bridge via ``@tool_worker(name="chat_llm")``;
  ADR-043). The legacy ``LiteLLMTool`` (Tool Protocol
  path) and the ``ToolInvoker`` orchestrator were
  removed in v0.9.0.

Concrete tools live in adapters — see
``kntgraph.agents/examples/12_invoice_issue_tool.py`` for a
worked example of writing one.

Iter 25: the framework moved ``walk_schema``,
``ToolRegistry``, ``ToolACL``, ``default_acl``, and
``ToolDescriptor`` into ``kntgraph.tools``. The
``kntgraph.agents.tools.protocol`` re-export shim keeps
existing imports working; new code should import
directly from ``kntgraph.tools``.
"""

from kntgraph.tools import (
    ToolACL,
    ToolDescriptor,
    ToolRegistry,
    default_acl,
)
from kntgraph.agents.tools.arg_validation import SchemaValidationError, validate_args
from kntgraph.agents.tools.capability import Capability
from kntgraph.agents.tools.pii import (
    DEFAULT_PII_LABELS,
    PiiRedactionTool,
    RedactionResult,
)
from kntgraph.agents.tools.protocol import (
    Callable,
    Describable,
    Tool,
    ToolArgValue,
    ToolCall,
    ToolEventType,
)
from kntgraph.tools.llm_transport import (
    LLMChunk,
    LLMResponse,
    LLMTransport,
    LLMUsage,
)
from kntgraph.agents.tools.llm import LiteLLMToolWorker


__all__ = [
    # Protocol / Registry / Events
    "Callable",
    "Capability",
    "Describable",
    "LLMChunk",
    "LLMResponse",
    "LLMTransport",
    "LLMUsage",
    "LiteLLMToolWorker",
    "PiiRedactionTool",
    "RedactionResult",
    "DEFAULT_PII_LABELS",
    "SchemaValidationError",
    "Tool",
    "ToolACL",
    "ToolArgValue",
    "ToolCall",
    "ToolDescriptor",
    "ToolEventType",
    "ToolRegistry",
    "default_acl",
    "validate_args",
]
