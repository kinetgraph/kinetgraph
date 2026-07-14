# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.components -- ECS components for the framework.

Components are frozen dataclasses installed on an
``AgentView`` by a projection. The materialised
state is the projection's responsibility; the
EventLog is the source of truth.
"""

from .memory import (
    ContinuityComponent,
    ProfileComponent,
    SessionComponent,
)

__all__ = [
    "ContinuityComponent",
    "ProfileComponent",
    "SessionComponent",
]
