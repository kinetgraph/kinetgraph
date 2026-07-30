# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``memory/continuity/pii.py``.

Closes the PII-gate coverage gap (DEBT §3, 60% → 100%).
The module centralises the rule that
``continuity.entity_seen`` MUST carry a sha256-truncated
hash, never a raw value (ADR-014 §2.7). The contract is
enforced by two helpers:

  - ``is_pii_hash(value)`` — cheap predicate.
  - ``check_pii_hash(value)`` — Railway-style validation
    that returns ``Err(PersistenceError)`` on a bad value.

Both branches of the strict check (valid + invalid) are
covered below.
"""

from __future__ import annotations

from kntgraph.core.result import PersistenceError
from kntgraph.memory.continuity.pii import (
    PII_HASH_PREFIX,
    check_pii_hash,
    is_pii_hash,
)


class TestIsPiiHash:
    def test_true_for_valid_prefix(self):
        assert is_pii_hash(f"{PII_HASH_PREFIX}abcdef") is True

    def test_true_for_long_hash(self):
        assert is_pii_hash(f"{PII_HASH_PREFIX}1234567890abcdef") is True

    def test_false_for_wrong_prefix(self):
        assert is_pii_hash("md5:abcdef") is False
        assert is_pii_hash("sha512:abcdef") is False
        assert is_pii_hash("plaintext") is False

    def test_false_for_empty_string(self):
        assert is_pii_hash("") is False

    def test_false_for_non_string(self):
        assert is_pii_hash(None) is False  # type: ignore[arg-type]
        assert is_pii_hash(123) is False  # type: ignore[arg-type]
        assert is_pii_hash(b"sha256:abc") is False  # type: ignore[arg-type]

    def test_prefix_is_sha256(self):
        assert PII_HASH_PREFIX == "sha256:"


class TestCheckPiiHash:
    def test_ok_for_valid_hash(self):
        result = check_pii_hash(f"{PII_HASH_PREFIX}abcdef0123456789")
        assert result.is_ok()

    def test_err_for_wrong_prefix(self):
        result = check_pii_hash("md5:abcdef")
        assert result.is_err()
        assert isinstance(result.err_value(), PersistenceError)
        assert "sha256:" in str(result.err_value())

    def test_err_for_empty_string(self):
        result = check_pii_hash("")
        assert result.is_err()
        assert isinstance(result.err_value(), PersistenceError)

    def test_err_for_plain_text(self):
        result = check_pii_hash("user@example.com")
        assert result.is_err()
        assert isinstance(result.err_value(), PersistenceError)

    def test_err_for_non_string(self):
        result = check_pii_hash(12345)  # type: ignore[arg-type]
        assert result.is_err()
        assert isinstance(result.err_value(), PersistenceError)

    def test_err_message_mentions_record_entity_seen(self):
        result = check_pii_hash("not-a-hash")
        assert result.is_err()
        assert "record_entity_seen" in str(result.err_value())
