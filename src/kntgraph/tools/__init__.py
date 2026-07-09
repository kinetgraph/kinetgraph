# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.tools -- framework-level Tool primitives.

Public surface
--------------

  - ``Describable`` (Protocol) -- identity metadata
  - ``Callable[T_in, T_out]`` (Protocol) -- async execution
  - ``Tool[R]`` (Protocol) -- full orchestration
  - ``FieldSpec``, ``walk_schema``, ``compute_schema_version``
    -- JSON-Schema view helpers
  - ``ToolDescriptor`` -- the static description of a Tool
  - ``ToolACL``, ``default_acl`` -- per-tool authorisation
  - ``ToolRegistry`` -- per-process registry with ACL
  - ``LLMTransport``, ``LLMRequest``, ``LLMResponse``,
    ``LLMUsage``, ``LLMChunk`` -- LLM I/O boundary
    (Iter 28 FU 3: LLMTransport is now a sub-Protocol
    of ``Callable[LLMRequest, dict]``).
  - ``tool_worker`` (decorator) -- mark a class as a
    Tool Worker (ADR-036 §2.4). The decorated class
    satisfies the ``Describable`` Protocol and
    registers with a ``WorkerManager``.
  - ``WorkerManager`` -- runs ``@tool_worker``-decorated
    tools in a ``ProcessPoolExecutor``, consuming
    ``knt:tools:<name>:queue`` Redis Streams via
    Consumer Groups (ADR-036 §2.2, §2.5).
  - ``ToolRouter`` -- fan-out helper that copies
    ``tool.requested`` events from the agent's
    EventLog to the global tool queue (ADR-036 §2.5).
    Wired into ``ReactiveDispatcher`` via
    ``tool_router=``; opt-in.
  - ``ToolAwareSystem`` -- mixin for ``WorldSystem``s
    that request tools and react to completions via
    the ``tool_requests`` / ``tool_completions`` ECS
    slots materialised by ``project_tool_calls`` /
    ``overlay_tool_calls`` (ADR-036 §2.3).

Vertical tools (``PiiRedactionTool``, ``LiteLLMTool``,
...) live in ``kntgraph.agents.tools`` and re-export the
Protocols from here.

The sub-package layout:

  - ``protocol.py`` -- the three Protocols.
  - ``schema.py`` -- JSON-Schema view helpers.
  - ``descriptors.py`` -- ``ToolDescriptor`` dataclass.
  - ``acl.py`` -- ``ToolACL`` dataclass + ``default_acl``.
  - ``registry.py`` -- ``ToolRegistry``.
  - ``llm_transport.py`` -- LLM I/O boundary (Iter 28 FU 3).
  - ``worker.py`` -- ``@tool_worker`` decorator (ADR-036).
  - ``manager.py`` -- ``WorkerManager`` (ADR-036).
  - ``router.py`` -- ``ToolRouter`` fan-out (ADR-036).
  - ``system.py`` -- ``ToolAwareSystem`` mixin (ADR-036).

Iter 25 closed the cycle that previously blocked
``kntgraph.agents.memory.knowledge_consolidator`` from
importing ``RedisLike`` (see ADR-019 §3.2, ADR-025).
The cycle ran through ``kntgraph.agents.tools.arg_validation
→ kntgraph.agents.knowledge.argument_extractor →
kntgraph.agents.knowledge.solution_projector →
kntgraph.agents.memory.solutions``; the framework-level
``walk_schema`` here is one of the two roots broken.
The second root (eager ``PiiRedactionTool`` import in
``_promoter.py``) is broken by typing the redactor
slot as a ``Callable`` instead of a ``Tool``.

Iter 28 FU 3: ``LLMTransport`` was a vertical Protocol
(in ``kntgraph.agents.tools.llm_transport``). The
framework's ``Callable`` Protocol is the canonical
shape for any async executable. Migrating
``LLMTransport`` to the framework closes the
duck-typed gap documented in Iter 25 §1.

Iter 36 / ADR-036: the four new modules
(``worker``, ``manager``, ``router``, ``system``)
implement the Tool Worker Pattern. The
``ReactiveDispatcher`` knows how to wire
``ToolRouter`` (opt-in via ``tool_router=``) and
applies ``overlay_tool_calls`` on every batch so
``ToolAwareSystem`` consumers see the ECS slots
without any subclassing.
"""

from __future__ import annotations

from .acl import ToolACL, default_acl
from .descriptors import ToolDescriptor
from .llm_transport import (
    LLMChunk,
    LLMRequest,
    LLMResponse,
    LLMTransport,
    LLMUsage,
)
from .manager import WorkerManager
from .protocol import (
    Callable,
    Describable,
    Tool,
    ToolArgValue,
)
from .registry import ToolRegistry
from .router import ToolRouter
from .schema import FieldSpec, compute_schema_version, walk_schema
from .system import ToolAwareSystem
from .worker import tool_worker


__all__ = [
    "Callable",
    "Describable",
    "FieldSpec",
    "LLMChunk",
    "LLMRequest",
    "LLMResponse",
    "LLMTransport",
    "LLMUsage",
    "Tool",
    "ToolACL",
    "ToolArgValue",
    "ToolAwareSystem",
    "ToolDescriptor",
    "ToolRegistry",
    "ToolRouter",
    "WorkerManager",
    "compute_schema_version",
    "default_acl",
    "tool_worker",
    "walk_schema",
]
