# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Knowledge tier sub-config (mixin).

Holds the consolidator cadence (ADR-021), the review
queue policy, and the argument extractor knobs.

``knowledge_interval_s`` — the post-tick loop interval
the consolidator uses to drain the EventLog.

``solutions_review_threshold`` — confidence floor
below which a candidate goes to the review queue
(default 0.7).

``solutions_review_queue`` — the Redis Stream key for
the human-review queue.

``solutions_review_ttl_s`` — how long an entry waits
in the review queue (default 7 days).

``solutions_tool_allowlist`` — CSV of tool names
eligible for promotion. Empty means every tool is
eligible.

``arg_threshold`` / ``arg_extractor_model_id`` — the
minimum confidence for a field to be kept by the
argument extractor, and the underlying model
identifier (when the optional ``[arg-gliner]`` extra
is installed).
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class KnowledgeSettingsMixin(BaseSettings):
    """Consolidator cadence, review queue, arg extractor."""

    knowledge_interval_s: float = Field(default=1.0, gt=0)
    solutions_review_threshold: float = Field(default=0.7, ge=0, le=1)
    solutions_review_queue: str = Field(default="knt:solutions:review")
    solutions_review_ttl_s: int = Field(default=7 * 24 * 60 * 60, gt=0)
    solutions_tool_allowlist: str = Field(default="")
    arg_threshold: float = Field(default=0.5, ge=0, le=1)
    # Default model is the public GLiNER2 base model.
    # Operators override per-deployment (private HF
    # checkpoint or local path). Was previously the
    # placeholder ``"default"`` (drifted from the real
    # classifier's hard-coded default ``"gliner2-base"``);
    # Iter 21 aligned the two values.
    arg_extractor_model_id: str = Field(default="gliner2-base")
