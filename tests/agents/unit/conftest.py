# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Shared pytest configuration for the agents/unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

# The example 05b module imports ``_lib.redis_or_fake``
# which lives in ``examples/_lib``. The unit tests
# load 05b by file path (importlib.util.spec_from_file_location)
# so we need to make ``_lib`` importable. We add the
# ``examples`` directory to ``sys.path`` here so the
# tests run with the default pytest invocation
# (``uv run pytest tests/agents/unit``).

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
if _EXAMPLES.is_dir() and str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))
