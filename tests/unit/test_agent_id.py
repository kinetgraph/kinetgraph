# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``core.agent_id`` — the central trust
boundary for ``agent_id``-shaped identifiers.

Pins the public contract:

  - ``MAX_AGENT_ID_LEN`` is the canonical cap (128).
  - ``AGENT_ID_RE`` matches ``[A-Za-z0-9._:-]{1,128}``.
  - ``validate_agent_id(value)`` returns ``None`` for
    valid input, a short error string for invalid input.
  - ``assert_valid_agent_id(value)`` raises for invalid
    input; the two flavours must agree on what "valid"
    means.

Existing tests in ``test_agent_id_validation.py`` cover
the end-to-end ``Event.__post_init__`` path; this
module covers the helpers directly so a future change
to the regex is caught even if ``Event`` is changed.
"""

from __future__ import annotations

import pytest

from kntgraph.core.agent_id import (
    AGENT_ID_RE,
    MAX_AGENT_ID_LEN,
    assert_valid_agent_id,
    validate_agent_id,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_max_agent_id_len_is_128(self):
        """The cap was a magic ``128`` repeated in three
        modules. Pin the public constant so a future
        change is intentional, not incidental.
        """
        assert MAX_AGENT_ID_LEN == 128

    def test_regex_matches_constant(self):
        """The regex's upper bound is derived from
        ``MAX_AGENT_ID_LEN`` — a one-liner can change
        one without the other, so pin the relationship.
        """
        assert f"{{1,{MAX_AGENT_ID_LEN}}}" in AGENT_ID_RE.pattern

    def test_regex_pattern_is_ascii_id(self):
        assert AGENT_ID_RE.pattern == r"^[A-Za-z0-9._:-]{1,128}$"


# ---------------------------------------------------------------------------
# validate_agent_id (returns Optional[str])
# ---------------------------------------------------------------------------


class TestValidateAgentId:
    @pytest.mark.parametrize(
        "value",
        [
            "a",
            "agent-1",
            "tenant-a.agent-1",
            "agent:r1",
            "session-2026-06-23T10:00:00Z",
            # Edge of the cap.
            "x" * MAX_AGENT_ID_LEN,
            # Dots, underscores, hyphens, colons are allowed.
            "a.b_c-d:e",
        ],
    )
    def test_valid_returns_none(self, value):
        assert validate_agent_id(value) is None

    @pytest.mark.parametrize(
        "value,fragment",
        [
            ("", "non-empty"),
            ("   ", "non-empty"),
            ("x" * (MAX_AGENT_ID_LEN + 1), "too long"),
            # Disallowed characters.
            ("agent 1", "characters outside"),
            ("agent/1", "characters outside"),
            ("agent*1", "characters outside"),
            ("agent\n1", "characters outside"),
            ("agent\t1", "characters outside"),
            ("agent@1", "characters outside"),
            # Unicode (non-ASCII).
            ("agént", "characters outside"),
        ],
    )
    def test_invalid_returns_short_string(self, value, fragment):
        err = validate_agent_id(value)
        assert err is not None
        # The message is short — no raw value leaked.
        assert len(err) < 80
        # Sanity: the fragment appears in the message so
        # operators can tell WHAT failed.
        assert fragment in err

    def test_non_str_returns_type_error_string(self):
        for value in [None, 42, 1.5, b"agent-1", ["a"], {"a": 1}]:
            err = validate_agent_id(value)
            assert err is not None
            assert "must be str" in err
            # The type name appears, not the value.
            assert type(value).__name__ in err

    def test_does_not_leak_raw_value(self):
        """Defence in depth: the error string MUST NOT
        include the raw input. Operators log the full
        value separately at ERROR level (see
        ``EventLog.append``); the wire message is what
        the client sees.
        """
        secret = "tenant-with-pii-12345678901"
        err = validate_agent_id(secret + " ")
        assert err is not None
        assert secret not in err


# ---------------------------------------------------------------------------
# assert_valid_agent_id (raises)
# ---------------------------------------------------------------------------


class TestAssertValidAgentId:
    @pytest.mark.parametrize(
        "value",
        [
            "a",
            "agent-1",
            "tenant-a.agent-1",
            "x" * MAX_AGENT_ID_LEN,
        ],
    )
    def test_valid_returns_none(self, value):
        assert assert_valid_agent_id(value) is None

    def test_non_str_raises_type_error(self):
        with pytest.raises(TypeError) as exc:
            assert_valid_agent_id(42)
        # Type info AND the value are in the message —
        # this path is in-process, the caller is trusted.
        assert "str" in str(exc.value)
        assert "int" in str(exc.value)

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            assert_valid_agent_id("")

    def test_whitespace_raises_value_error(self):
        with pytest.raises(ValueError):
            assert_valid_agent_id("   ")

    def test_too_long_raises_value_error(self):
        with pytest.raises(ValueError) as exc:
            assert_valid_agent_id("x" * (MAX_AGENT_ID_LEN + 1))
        assert str(MAX_AGENT_ID_LEN) in str(exc.value)

    @pytest.mark.parametrize(
        "value",
        ["agent 1", "agent/1", "agent\n1", "agént"],
    )
    def test_bad_chars_raise_value_error(self, value):
        with pytest.raises(ValueError) as exc:
            assert_valid_agent_id(value)
        assert "must match" in str(exc.value)


# ---------------------------------------------------------------------------
# Cross-check: the two flavours must agree
# ---------------------------------------------------------------------------


class TestCrossCheck:
    @pytest.mark.parametrize(
        "value",
        [
            "agent-1",
            "tenant-a.agent-1",
            "x" * MAX_AGENT_ID_LEN,
            "agent 1",
            "agent*1",
            "",
            "x" * (MAX_AGENT_ID_LEN + 1),
            42,
        ],
    )
    def test_validate_and_assert_agree(self, value):
        """``validate_agent_id`` says valid ⇔
        ``assert_valid_agent_id`` does not raise. This
        pins that the two flavours are not silently
        diverging.
        """
        validate_result = validate_agent_id(value)
        if validate_result is None:
            # Valid in validate_* — must also be valid
            # in assert_*.
            assert_valid_agent_id(value)
        else:
            # Invalid in validate_* — assert_* must raise
            # the same kind (TypeError for non-str,
            # ValueError for everything else).
            with pytest.raises((TypeError, ValueError)):
                assert_valid_agent_id(value)
