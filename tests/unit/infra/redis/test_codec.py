# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/redis/_codec.py``.

Closes the infra/redis/_codec coverage gap (DEBT §3,
64% → 100%). The module centralises the
bytes→str coercion at the Redis read boundary in
three pure functions:

  - ``decode_value`` — single value (bytes / str /
    None) → str / None.
  - ``decode_dict`` — dict[bytes|str, bytes|str] →
    dict[str, str]. ``None`` values coerce to ``""``
    (the contract — empty string is the sentinel for
    "Redis returned no value for this field").
  - ``decode_int_dict`` — same as ``decode_dict`` but
    values are coerced to ``int``; values that cannot
    be parsed fall back to ``0`` (the codec is
    defensive — a malformed integer must not crash
    the call site, the manager treats ``0`` as the
    "missing" sentinel).
"""

from __future__ import annotations

from kntgraph.infra.redis._codec import (
    decode_dict,
    decode_int_dict,
    decode_value,
)


# ---------------------------------------------------------------------------
# decode_value
# ---------------------------------------------------------------------------


class TestDecodeValue:
    def test_none_returns_none(self) -> None:
        assert decode_value(None) is None

    def test_bytes_is_decoded_as_utf8(self) -> None:
        assert decode_value(b"hello") == "hello"

    def test_str_passes_through(self) -> None:
        assert decode_value("hello") == "hello"

    def test_empty_bytes(self) -> None:
        assert decode_value(b"") == ""

    def test_empty_str(self) -> None:
        assert decode_value("") == ""

    def test_unicode_bytes(self) -> None:
        assert decode_value("olá".encode("utf-8")) == "olá"


# ---------------------------------------------------------------------------
# decode_dict
# ---------------------------------------------------------------------------


class TestDecodeDict:
    def test_bytes_key_and_value(self) -> None:
        assert decode_dict({b"k": b"v"}) == {"k": "v"}

    def test_str_key_and_value(self) -> None:
        assert decode_dict({"k": "v"}) == {"k": "v"}

    def test_mixed_bytes_and_str(self) -> None:
        assert decode_dict({b"a": "b", "c": b"d"}) == {"a": "b", "c": "d"}

    def test_none_value_coerced_to_empty_string(self) -> None:
        # The contract: a Redis field that returned no
        # value is decoded as ``""`` (not skipped) so
        # the manager's downstream code does not have
        # to special-case missing fields.
        assert decode_dict({"k": None}) == {"k": ""}

    def test_none_key_is_skipped(self) -> None:
        # A ``None`` key is a Redis-protocol error
        # (Redis never returns None keys), but the
        # codec drops the entry rather than crashing.
        assert decode_dict({None: "v"}) == {}

    def test_empty_dict(self) -> None:
        assert decode_dict({}) == {}

    def test_unicode_values(self) -> None:
        assert decode_dict({"name": "João".encode("utf-8")}) == {"name": "João"}


# ---------------------------------------------------------------------------
# decode_int_dict
# ---------------------------------------------------------------------------


class TestDecodeIntDict:
    def test_bytes_value_is_coerced(self) -> None:
        assert decode_int_dict({b"count": b"42"}) == {"count": 42}

    def test_str_numeric_value_is_coerced(self) -> None:
        assert decode_int_dict({"count": "42"}) == {"count": 42}

    def test_int_value_passes_through(self) -> None:
        assert decode_int_dict({"count": 42}) == {"count": 42}

    def test_none_value_coerced_to_zero(self) -> None:
        assert decode_int_dict({"count": None}) == {"count": 0}

    def test_unparseable_value_falls_back_to_zero(self) -> None:
        # The contract: a malformed integer must not
        # crash the call site. ``0`` is the "missing"
        # sentinel for the manager.
        assert decode_int_dict({"count": "not-a-number"}) == {"count": 0}

    def test_unparseable_bytes_falls_back_to_zero(self) -> None:
        assert decode_int_dict({b"count": b"abc"}) == {"count": 0}

    def test_empty_dict(self) -> None:
        assert decode_int_dict({}) == {}

    def test_none_key_is_skipped(self) -> None:
        assert decode_int_dict({None: 1}) == {}

    def test_mixed_keys(self) -> None:
        assert decode_int_dict({b"a": 1, "b": "2"}) == {"a": 1, "b": 2}
