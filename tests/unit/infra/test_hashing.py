# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/hashing.py``.

Closes the infra/hashing coverage gap (DEBT §3,
92% → 100%). The module exposes ``short_hash`` (the
SHA-256-truncated pattern used as a fingerprint
across the framework) and the ``DEFAULT_HASH_LEN``
constant. The uncovered branch was the
``length >= len(digest)`` arm (a caller asking for
more than the full digest — the helper returns the
complete digest rather than padding).
"""

from __future__ import annotations

import hashlib

from kntgraph.infra.hashing import DEFAULT_HASH_LEN, short_hash


class TestShortHash:
    def test_default_length_is_16(self) -> None:
        assert DEFAULT_HASH_LEN == 16

    def test_str_input_produces_16_char_hex(self) -> None:
        h = short_hash("hello")
        assert len(h) == 16
        # Stable across calls.
        assert h == short_hash("hello")

    def test_bytes_input_produces_16_char_hex(self) -> None:
        h = short_hash(b"hello")
        assert len(h) == 16
        assert h == short_hash("hello")

    def test_str_and_bytes_equivalent(self) -> None:
        assert short_hash("hello") == short_hash(b"hello")

    def test_custom_length(self) -> None:
        h = short_hash("hello", length=8)
        assert len(h) == 8

    def test_length_greater_than_full_digest_returns_full_digest(
        self,
    ) -> None:
        # The full SHA-256 hex digest is 64 chars. A
        # caller asking for ``length=128`` gets the
        # full digest rather than a padded value.
        full = hashlib.sha256(b"hello").hexdigest()
        assert len(full) == 64
        h = short_hash("hello", length=128)
        assert h == full

    def test_length_equal_to_full_digest_returns_full_digest(self) -> None:
        # The boundary: ``length == 64`` is NOT a
        # truncation (the contract is ``< len``).
        full = hashlib.sha256(b"hello").hexdigest()
        h = short_hash("hello", length=64)
        assert h == full

    def test_distinct_inputs_distinct_hashes(self) -> None:
        assert short_hash("a") != short_hash("b")
