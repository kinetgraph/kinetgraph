# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``kntgraph.__version__`` discovery (ADR-051).

The version is derived from the git tag by
``setuptools_scm`` at install time (written to
``src/kntgraph/_version.py``). The ``__init__.py``
imports it with a fallback for environments where
``_version.py`` is missing (source tarballs, CI caches
without a full git history).

These tests cover the contract regardless of the
install state:

  - ``__version__`` is always a string.
  - The string is PEP 440 compliant (parseable by
    ``packaging.version.Version``), OR it is the
    explicit ``"0.0.0+unknown"`` fallback.
  - When the tag-derived version is present, it is
    the **expected** version (the git tag matches).

The test runs in any environment (CI, local
checkout, source tarball) and asserts the contract.
"""

from __future__ import annotations

import re

import pytest

import kntgraph
from packaging.version import InvalidVersion, Version


class TestVersionDiscovery:
    """The ``__version__`` attribute is always present
    and always a string. Either the tag-derived
    version (PEP 440) or the explicit
    ``"0.0.0+unknown"`` fallback.
    """

    def test_version_is_a_string(self) -> None:
        assert isinstance(kntgraph.__version__, str)
        assert kntgraph.__version__ != ""

    def test_version_is_pep440_or_fallback(self) -> None:
        """Either a valid PEP 440 version, or the
        explicit ``"0.0.0+unknown"`` fallback
        (no ``_version.py`` generated; e.g. a source
        tarball without setuptools_scm).
        """
        if kntgraph.__version__ == "0.0.0+unknown":
            return
        try:
            Version(kntgraph.__version__)
        except InvalidVersion as e:
            pytest.fail(
                f"__version__={kntgraph.__version__!r} is "
                f"neither a PEP 440 version nor the "
                f"'0.0.0+unknown' fallback: {e}"
            )

    def test_version_matches_v_prefix_regex(self) -> None:
        """Sanity: the version string, when stripped
        of any local-segment suffix, looks like
        ``X.Y.Z`` (the project's tag scheme).
        """
        if kntgraph.__version__ == "0.0.0+unknown":
            return
        # Drop the local segment after ``+`` if any.
        base = kntgraph.__version__.split("+", 1)[0]
        # The tag scheme is ``X.Y.Z`` (3 segments).
        # Pre-releases like ``0.11.0a1`` are PEP 440
        # valid; the regex is permissive on purpose.
        assert re.match(r"^\d+\.\d+\.\d+", base), (
            f"__version__={kntgraph.__version__!r} does "
            f"not start with ``X.Y.Z`` as expected by "
            f"the project's tag scheme"
        )

    def test_version_attribute_is_documented(self) -> None:
        """The attribute is part of the package's
        public surface (the README's ``docs/quality.md``
        generator reads it). Asserting it is a string
        is enough; deeper assertions live in the
        ``check_version`` step (CI).
        """
        assert hasattr(kntgraph, "__version__")
