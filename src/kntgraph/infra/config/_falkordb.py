# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
FalkorDB sub-config (mixin).

Holds the connection endpoint and password. Password
is ``Optional`` so dev instances without auth work out
of the box.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class FalkordbSettingsMixin(BaseSettings):
    """Connection host, port, and optional password."""

    falkordb_host: str = Field(default="localhost")
    falkordb_port: int = Field(default=16379)
    falkordb_password: Optional[str] = Field(default=None)
