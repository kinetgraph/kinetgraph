# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Streams sub-config (mixin).

Redis Stream ``MAXLEN`` knobs. ``stream_maxlen`` is the
per-tenant cap; ``global_stream_maxlen`` is the global
fallback. Operators typically size these based on
expected write rate and storage budget.
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class StreamsSettingsMixin(BaseSettings):
    """Per-tenant and global Stream MAXLEN caps."""

    stream_maxlen: int = Field(default=100000)
    global_stream_maxlen: int = Field(default=1000000)
