# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Re-export of the framework-level
``kntgraph.tools.llm_transport`` module (Iter 28
FU 3).

The canonical home of ``LLMTransport``, ``LLMRequest``,
``LLMResponse``, ``LLMUsage``, and ``LLMChunk`` is
the framework. This module re-exports them for
backward compat: callers that did
``from kntgraph.agents.tools.llm_transport import ...``
keep working.

Iter 28 FU 3 closed the duck-typed gap between
``LLMTransport`` and the framework's ``Callable``
Protocol (Iter 25). The transport is now a
structural match for ``Callable[LLMRequest, dict]``
by inheriting the same ``__call__`` shape.
"""

from __future__ import annotations

from kntgraph.tools.llm_transport import (
    LLMChunk as LLMChunk,
)
from kntgraph.tools.llm_transport import (
    LLMRequest as LLMRequest,
)
from kntgraph.tools.llm_transport import (
    LLMResponse as LLMResponse,
)
from kntgraph.tools.llm_transport import (
    LLMTransport as LLMTransport,
)
from kntgraph.tools.llm_transport import (
    LLMUsage as LLMUsage,
)


__all__ = [
    "LLMChunk",
    "LLMRequest",
    "LLMResponse",
    "LLMTransport",
    "LLMUsage",
]
