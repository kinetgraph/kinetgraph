# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
HTTP client adapter sub-package.

Public API
----------

- :class:`HttpClientLike` / :class:`HttpResponseLike` --
  framework-level Protocol for an async HTTP client
  (ADR-047 §2.2.2 "Abstract via Protocol").
- :class:`HttpxHttpClientAdapter` -- ``httpx.AsyncClient``
  implementation. The ``httpx`` import is **lazy** so
  the framework's import graph stays clean when the
  operator does not need HTTP at all.

This module is the framework's HTTP I/O boundary. The
canonical use case is ``@tool_worker`` classes that
need to call an external REST API (e.g.
``OpenMeteoApi`` in the weather platform vertical).
Per ADR-047, the ``ToolWorker`` MUST NOT import a
concrete HTTP client; it accepts a ``HttpClientLike``
via its ``__init__``.
"""

from ._client import (
    HttpClientLike,
    HttpResponseLike,
    HttpxHttpClientAdapter,
)

__all__ = [
    "HttpxHttpClientAdapter",
    "HttpClientLike",
    "HttpResponseLike",
]
