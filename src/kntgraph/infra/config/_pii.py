# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
PII redaction sub-config (mixin).

Holds the redaction level knob consumed by
``kntgraph.agents.tools.pii.PiiRedactionTool`` when the
level is not passed at construction.

  - 1 = regex (default)
  - 2 = NER (heuristic or GLiNER2)
  - 3 = GLiNER2 audit batch

``None`` (default) defers to the tool's own default
(level 1) so the framework can run without any
PII-specific env.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class PiiSettingsMixin(BaseSettings):
    """Default PII redaction level (1=regex, 2=NER, 3=audit)."""

    pii_level: Optional[int] = Field(default=None, ge=1, le=3)
