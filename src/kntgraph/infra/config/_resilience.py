# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Resilience sub-config (mixin).

Holds circuit-breaker tuning and retry policy. The two
are coupled in practice (a circuit breaker stops
issuing retries once it opens) but the knobs are
independent — a deployment may want aggressive retries
without a breaker, or a tight breaker with no retries.
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class ResilienceSettingsMixin(BaseSettings):
    """Circuit-breaker thresholds and retry policy."""

    circuit_breaker_threshold: int = Field(default=5)
    circuit_breaker_timeout: int = Field(default=30)
    retry_max_attempts: int = Field(default=3)
    retry_base_delay: float = Field(default=2.0)
