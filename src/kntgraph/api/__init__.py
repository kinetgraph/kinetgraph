# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
HTTP gateway (ADR-012).

The `kntgraph.api` package is an **opt-in** adapter that
exposes the framework over HTTP. It produces `tool.{name}.requested`
events into the existing `EventLog`; the rest of the framework
(`ToolInvoker`, `ToolRegistry`, etc.) is unchanged.

Importing this package requires the `[api]` extra
(`fastapi`, `uvicorn`). The framework's core does not
depend on FastAPI.
"""

from .intent_router import app, create_app

__all__ = [
    "app",
    "create_app",
]
