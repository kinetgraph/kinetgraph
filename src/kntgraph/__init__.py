# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph -- public package surface.

The ``__version__`` attribute is derived from the
git tag by ``setuptools_scm`` (ADR-051). The import
is guarded so a source install without the build
step (e.g. a CI cache without a full git history)
returns the explicit ``"0.0.0+unknown"`` fallback
instead of raising ``AttributeError``.

The fallback uses PEP 440's local-version
convention (``+unknown``) so consumers can detect
the case programmatically via
``version.endswith("+unknown")``.
"""

from __future__ import annotations

__all__ = ["__version__"]

try:
    from kntgraph._version import __version__
except ImportError:
    # Source install without ``setuptools_scm``
    # having run (no git history, no tag). The
    # version is unknown; downstream code that
    # relies on a real version should fall back
    # gracefully rather than crash.
    __version__ = "0.0.0+unknown"
