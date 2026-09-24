# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Branch coverage for ``continuity/cache_codec.py`` coerce
helpers (lines 173-198).

These are SYNCHRONOUS unit tests for pure helper functions.
They live in their own file (not ``test_managers_unit.py``)
because the latter declares a module-level
``pytestmark = pytest.mark.asyncio`` for the manager tests;
mixing async and sync under the same mark triggers
``PytestWarning: ... marked with '@pytest.mark.asyncio' but it is
not an async function``.

Branch tests for the managers (Session, Profile, Continuity)
remain in ``test_managers_unit.py`` under the asyncio mark.
"""

from __future__ import annotations


class TestContinuityCacheCodecBranches:
    """Branch coverage for ``continuity/cache_codec.py``
    coerce helpers (lines 173-198)."""

    def test_coerce_float_or_none_with_bool(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none(True) == 1.0
        assert _coerce_float_or_none(False) == 0.0

    def test_coerce_float_or_none_with_int(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none(42) == 42.0

    def test_coerce_float_or_none_with_float(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none(3.14) == 3.14

    def test_coerce_float_or_none_with_non_scalar(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none([1.0]) is None
        assert _coerce_float_or_none({"x": 1}) is None

    def test_coerce_float_or_zero_with_bool(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_zero

        assert _coerce_float_or_zero(True) == 1.0
        assert _coerce_float_or_zero(False) == 0.0

    def test_coerce_float_or_zero_with_int(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_zero

        assert _coerce_float_or_zero(42) == 42.0

    def test_coerce_float_or_zero_with_non_scalar(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_zero

        assert _coerce_float_or_zero([1.0]) == 0.0
        assert _coerce_float_or_zero({"x": 1}) == 0.0

    def test_coerce_float_or_none_with_non_numeric_string(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_none

        assert _coerce_float_or_none("not-a-float") is None

    def test_coerce_float_or_zero_with_non_numeric_string(self) -> None:
        from kntgraph.memory.continuity.cache_codec import _coerce_float_or_zero

        assert _coerce_float_or_zero("not-a-float") == 0.0
