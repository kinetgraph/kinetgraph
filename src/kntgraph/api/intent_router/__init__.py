# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
IntentRouter — the HTTP gateway (ADR-012).

The router is a thin producer of `tool.{name}.requested`
events. It does not run any Tool, Role, or business
logic. Its responsibilities are:

  1. **Authentication** — `X-API-Key` header → `agent_id`.
  2. **Schema validation** — pydantic models.
  3. **Tool/Role lookup** — reject 404 if the name is
     not in the `ToolRegistry`. NO event is emitted
     on rejection; the EventLog stays clean.
  4. **Event emission** — generate a deterministic
     `event_id` from the request, append a
     `tool.{name}.requested` event to the EventLog.
  5. **Status read** — long-poll the EventLog for the
     terminal event whose `causation_id == event_id`.

The router is **one** adapter. The framework still
runs without it: the `ToolInvoker` continues to
consume events from the EventLog regardless of how
they were produced.

This module is a thin facade. The implementation is
split across the `intent_router` subpackage:

  - `intent_router.app_factory`    — `create_app` /
    `_create_app` / `_build_app` (the FastAPI import
    boundary and the route composition).
  - `intent_router.routes`         — `register_healthz`
    / `register_list_tools` /
    `register_post_intent` /
    `register_get_status`.
  - `intent_router.middleware_setup`— `configure_middlewares`
    (B4 + B5).
  - `intent_router.helpers`        — pure utilities
    (private helpers plus the ``_INTENT_NS`` UUID5
    namespace and the idempotency-key trust-boundary
    constants).

The module-level ``app`` is intentionally ``None`` at
import time. Production deployments call
``create_app(...)`` with a populated registry and a
configured verifier.
"""

from .app_factory import _create_app, create_app
from .helpers import (
    _IDEMPOTENCY_KEY_BAD_CHARS,
    _INTENT_NS,
    _MAX_IDEMPOTENCY_KEY_LEN,
)


# Module-level ``app`` is intentionally ``None`` at
# import time. Production deployments call
# ``create_app(...)`` with a populated registry and a
# configured verifier (see example
# ``10_http_intent_router.py``). The framework does NOT
# auto-build an ``app`` because that would require
# knowing the EventLog backend and the auth scheme,
# both of which are deployment-specific.
#
# The annotation is untyped intentionally: the module
# must remain importable without FastAPI installed
# (the ``[api]`` extra pulls FastAPI in). The runtime
# value is either ``None`` (the default) or a
# ``fastapi.FastAPI`` instance, but the framework
# treats it as opaque at the module level. Callers
# that need the typed handle should call
# :func:`create_app` directly rather than reading
# this attribute.
app = None


__all__ = [
    "_IDEMPOTENCY_KEY_BAD_CHARS",
    "_INTENT_NS",
    "_MAX_IDEMPOTENCY_KEY_LEN",
    "_create_app",
    "app",
    "create_app",
]
