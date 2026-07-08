# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Runner sub-config (mixin).

Holds the post-tick loop interval — the smallest unit
of time the framework uses to schedule work.
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class RunnerSettingsMixin(BaseSettings):
    """Post-tick loop interval in seconds."""

    tick_interval: float = Field(default=1.0)
