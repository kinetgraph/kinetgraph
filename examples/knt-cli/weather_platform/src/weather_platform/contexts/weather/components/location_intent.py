# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass

from kntgraph.core.world import DomainComponent


@dataclass(frozen=True, slots=True)
class LocationIntent(DomainComponent):
    """
    ECS Component: LocationIntent.

    A domain component: the agent's rules read it **by this
    exact class** (ADR-088 §3.3).
    """

    city: str
    latitude: float
    longitude: float
