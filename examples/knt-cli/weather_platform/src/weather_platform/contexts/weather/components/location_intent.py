# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LocationIntent:
    """
    ECS Component: LocationIntent.
    """

    city: str
    latitude: float
    longitude: float
