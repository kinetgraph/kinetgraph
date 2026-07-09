# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration for kntgraph.agents tests.

Adds kntgraph and kntgraph.agents source paths so tests run
without requiring `pip install -e .`. Picked up by pytest
because conftest.py at the tests root is auto-loaded.
"""

import sys
from pathlib import Path

# kntgraph/src
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "kntgraph" / "src"))
# kntgraph.agents/src
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
