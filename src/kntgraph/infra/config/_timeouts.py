# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Timeout sub-config (mixin).

The framework uses two timeout knobs: a generic
``default_timeout`` for unspecified I/O, and an
``llm_timeout`` for LLM calls specifically (typically
slower than the default).
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class TimeoutsSettingsMixin(BaseSettings):
    """Default and LLM-specific timeouts (seconds)."""

    default_timeout: float = Field(default=10.0)
    llm_timeout: float = Field(default=30.0)
