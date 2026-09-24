# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.testing -- shared test fixtures and helpers.

This package is part of the framework's testing surface.
Code here is consumed by both unit and integration tests
to avoid each test re-implementing common fakes.

Public surface:

  - ``embedding.FakeEmbeddingProvider`` — deterministic,
    dependency-free ``EmbeddingProvider`` for tests that
    need a vector-producing object without Ollama.
  - ``world_builder.AgentViewBuilder`` / ``WorldBuilder`` —
    fluent SUT builders that assemble a ``World`` (and its
    ``AgentView``s) without mocks, Redis, or fabricated
    ``Event`` envelopes. ``world_builder.run_system`` invokes
    a ``WorldSystem`` inside a correlation scope (ADR-037).
"""

from .clock import fixed_now
from .embedding import FakeEmbeddingProvider
from .world_builder import (
    AgentViewBuilder,
    WorldBuilder,
    assert_all_correlation_ids,
    assert_correlation_id,
    run_system,
)

__all__ = [
    "AgentViewBuilder",
    "FakeEmbeddingProvider",
    "WorldBuilder",
    "assert_all_correlation_ids",
    "assert_correlation_id",
    "fixed_now",
    "run_system",
]
