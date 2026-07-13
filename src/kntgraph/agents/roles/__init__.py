# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
.. deprecated::
    The ``kntgraph.agents.roles`` package is **deprecated** and will be
    removed in **v1.0** (target: end of 2026 Q4). It has been
    superseded by the pure-ECS architecture defined in
    `ADR-039 <../../../ADRs/ADR-039-Role-rethinking-and-intentions-routing.md>`_:
    a semantic *Role* is now a ``RoleComponent`` (immutable data) and
    intent execution is handled by the pure ``IntentResolutionSystem``
    (no LLM I/O in the ``__call__`` cycle).

    Migration path
    --------------
    Replace role invocations with the new pipeline:

      1. Define a ``RoleComponent`` with the persona, system prompt,
         and permitted tool inventory.
      2. Emit an ``IntentComponent`` describing what the user wants.
      3. Let the ``IntentResolutionSystem`` resolve the intent into
         a tool call (Zero-Trust ACL + semantic capability check).

    Importing this package emits a :class:`DeprecationWarning` since
    v0.8.0. The package is kept alive through v0.9 to give downstream
    code time to migrate.

See also
--------
ADR-039 (Role rethinking and intent routing)
ADR-040 (Messaging adapter for intent ingestion)
"""

from __future__ import annotations

import warnings

_warned = False


def _emit_deprecation_warning() -> None:
    """Emit the package-level deprecation warning at most once per process."""
    global _warned
    if _warned:
        return
    _warned = True
    warnings.warn(
        "kntgraph.agents.roles is deprecated since v0.8.0 and will be "
        "removed in v1.0. Migrate to RoleComponent + IntentResolutionSystem "
        "(see ADR-039).",
        DeprecationWarning,
        stacklevel=3,
    )


_emit_deprecation_warning()

from .chat import ChatReply, ChatRole  # noqa: E402,F401
from .personalized import PersonalizedRole  # noqa: E402,F401
from .planner import Plan, PlannerRole, PlanStep  # noqa: E402,F401
from .resolution import (  # noqa: E402,F401
    IntentComponent,
    IntentResolutionSystem,
    RoleComponent,
)
from .semantic_router import (  # noqa: E402,F401
    EVENT_TYPE_ROUTING_UNCLASSIFIED,
    EVENT_TYPE_USER_MESSAGE,
    RoutingConfig,
    RoutingDecision,
    SemanticRoutingRole,
    async_route_on_user_message,
    route_on_user_message,
)
from .summarizer import SummarizerRole, Summary  # noqa: E402,F401

__all__ = [
    "ChatReply",
    "ChatRole",
    "PersonalizedRole",
    "Plan",
    "PlannerRole",
    "PlanStep",
    "IntentComponent",
    "IntentResolutionSystem",
    "RoleComponent",
    "EVENT_TYPE_ROUTING_UNCLASSIFIED",
    "EVENT_TYPE_USER_MESSAGE",
    "RoutingConfig",
    "RoutingDecision",
    "SemanticRoutingRole",
    "async_route_on_user_message",
    "route_on_user_message",
    "SummarizerRole",
    "Summary",
]
