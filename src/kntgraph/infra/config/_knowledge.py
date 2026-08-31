# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Knowledge tier sub-config (mixin).

Holds the consolidator cadence (ADR-021), the review
queue policy, and the argument extractor knobs.

``knowledge_interval_s`` — post-tick interval the
consolidator uses to drain the EventLog (ADR-021).

``solutions_review_threshold`` / ``solutions_review_queue``
/ ``solutions_review_ttl_s`` / ``solutions_tool_allowlist``
— review queue policy and tool allowlist.

``arg_threshold`` / ``arg_extractor_model_id`` —
argument-extractor confidence floor and GLiNER2 model
identifier (loaded by ``GlinerModelRegistry``).

``model_cache_dir`` — local path for HuggingFace model
weights, forwarded as ``cache_dir`` by the
``GlinerModelRegistry``. See ADR-055 §2.1 for the
cache-key contract and the ``HF_HOME`` fallback note.
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
    # Local cache directory for HuggingFace model weights
    # (ADR-055). ``None`` defers to the HF default. Env
    # var: ``KNT_MODEL_CACHE_DIR``.
    model_cache_dir: str | None = Field(default=None)
