# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Embedding tier sub-config (mixin).

Holds the embedding knobs that the framework exposes
end-to-end:

  - ``embedding_default_model`` — the model tag
    passed to ``OllamaEmbeddingAdapter(model=...)``
    (or any other impl) when the caller does not
    specify one.
  - ``embedding_default_dimension`` — the dimension
    the vector index expects. **MUST match** the
    model's actual output dimension; a mismatch
    causes the FalkorDB ``vecf32(...)`` insertion
    to reject the vector at runtime.
  - ``embedding_timeout_seconds`` — per-call timeout
    for the embed operation. The transport's
    ``asyncio.to_thread`` is bounded by this; on
    timeout the caller (e.g. ``FalkorDBProjector``)
    sees an exception.

Why a mixin (not a free-standing Settings)
-----------------------------------------

Same rationale as ``LLMSettingsMixin``: the
framework's ``Settings`` is a single canonical
config, with sub-configs mixed in.

Invariant
---------

The default model and dimension MUST be kept in
sync. If you change ``embedding_default_model``
to one that returns a different dimension, you
MUST change ``embedding_default_dimension`` in
the same commit. The pair is loaded at
``Settings()`` construction; a runtime check
between them is intentionally absent (the check
would require importing the model, which
defeats the purpose of a static config).
"""

from __future__ import annotations

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class EmbeddingSettingsMixin(BaseSettings):
    """Embedding model + dimension + timeout knobs."""

    # Model selection. ``paraphrase-multilingual``
    # is the framework's default (768d, multilingual).
    embedding_default_model: str = Field(default="paraphrase-multilingual")
    # Dimension. MUST match the model's actual output.
    # For ``paraphrase-multilingual`` (the
    # sentence-transformers MiniLM-L12-v2 model), the
    # output is 768.
    embedding_default_dimension: int = Field(default=768, gt=0)
    # Per-call timeout. The Ollama adapter's
    # ``asyncio.to_thread`` is bounded by this; on
    # timeout the call raises (the framework
    # translates to ``Result[Err(...)]`` at the
    # call site).
    embedding_timeout_seconds: float = Field(default=5.0, gt=0)
