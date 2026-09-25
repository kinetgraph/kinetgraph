# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for :mod:`kntgraph.infra.redis._prefix` -- the
namespace-prefix helper (ADR-076).

The helper is the single source of truth for the
``KNT_REDIS_KEY_PREFIX`` env var. Two functions to cover:

  - :func:`validate_prefix` -- raises ``ValueError`` on a
    malformed prefix.
  - :func:`namespaced` -- composition rule (empty prefix
    is the no-op fast path).

The tests target the **contract** (what operators get
when they set the env var), not the implementation; if
the regex changes, the assertions below still hold as
long as the documented behaviour is preserved.
"""

from __future__ import annotations

import pytest

from kntgraph.infra.redis._prefix import namespaced, validate_prefix


# ---------------------------------------------------------------------------
# validate_prefix
# ---------------------------------------------------------------------------


class TestValidatePrefix:
    """The validator's contract.

    Empty string is always valid (the default; no
    behaviour change vs pre-076). Non-empty must match
    the documented allow-list. A bare ``":"`` is rejected
    (looks like an oversight). Anything outside the
    allow-list raises.
    """

    def test_empty_string_is_valid(self) -> None:
        """Default; pre-076 deployments keep working."""
        validate_prefix("")  # does not raise

    @pytest.mark.parametrize(
        "prefix",
        [
            "acme",  # bare identifier
            "acme-billing",  # dash
            "acme_billing",  # underscore
            "acme.billing",  # dot
            "acme:",  # trailing colon (operator wants a separator)
            "tenant1.v2",  # dot-separated version
            "ABC123",  # uppercase + digits
            "a-b_c.d:e",  # all four special chars
        ],
    )
    def test_allowed_prefixes_pass(self, prefix: str) -> None:
        """Every documented separator is valid."""
        validate_prefix(prefix)

    @pytest.mark.parametrize(
        "prefix",
        [
            ":",  # bare separator (looks like an oversight)
            "acme*",  # glob wildcard -- would break SCAN patterns
            "acme{foo}",  # format-template syntax -- would break ``.format``
            "acme billing",  # whitespace
            "acme\tbilling",  # tab
            "açme",  # non-ASCII letter
            "acme/billing",  # forward slash
        ],
    )
    def test_invalid_prefixes_raise(self, prefix: str) -> None:
        """Anything outside the allow-list raises ``ValueError``."""
        with pytest.raises(ValueError, match="redis_key_prefix"):
            validate_prefix(prefix)

    def test_non_string_raises(self) -> None:
        """Defence-in-depth: non-string input raises ``TypeError``/``ValueError``.

        The Pydantic ``field_validator`` on
        ``RedisSettingsMixin`` already rejects non-strings
        before this function sees them, but the helper
        itself must not assume a clean string -- an
        operator constructing a ``RedisPool`` by hand
        could pass anything.
        """
        with pytest.raises((TypeError, ValueError)):
            validate_prefix(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# namespaced
# ---------------------------------------------------------------------------


class TestNamespaced:
    """The composition rule.

    Empty prefix is the no-op fast path (byte-for-byte
    identical to the input). Non-empty prefix concatenates
    at the front with no separator (the operator is
    responsible for the trailing colon if they want one).
    """

    def test_empty_prefix_returns_key_unchanged(self) -> None:
        """Pre-076 wire format: no change."""
        assert namespaced("", "knt:agents:a-1:events") == "knt:agents:a-1:events"

    def test_non_empty_prefix_concatenates(self) -> None:
        """Simple concatenation at the front."""
        assert (
            namespaced("acme-billing:", "knt:agents:a-1:events")
            == "acme-billing:knt:agents:a-1:events"
        )

    def test_prefix_without_trailing_colon(self) -> None:
        """No automatic separator. Operator's responsibility."""
        assert namespaced("acme", "knt:dlq:events") == "acmeknt:dlq:events"

    @pytest.mark.parametrize(
        "prefix,key,expected",
        [
            ("", "knt:dlq:events", "knt:dlq:events"),
            ("p1:", "knt:dlq:events", "p1:knt:dlq:events"),
            ("p1.p2:", "knt:dlq:events", "p1.p2:knt:dlq:events"),
            (
                "tenant-x.service-y:",
                "knt:agents:a-1:events",
                "tenant-x.service-y:knt:agents:a-1:events",
            ),
        ],
    )
    def test_property_table(self, prefix: str, key: str, expected: str) -> None:
        """Spot-check several prefix/key combinations.

        Acts as a property table: if anyone changes the
        composition rule (e.g. adds an automatic ``:``
        separator), the diff lands here first.
        """
        assert namespaced(prefix, key) == expected

    def test_idempotent_under_empty_prefix(self) -> None:
        """Double-wrap with an empty prefix is a no-op.

        Operators who conditionally apply the prefix
        (``namespaced(settings.redis_key_prefix, key)``)
        rely on this when the prefix is unset.
        """
        key = "knt:agents:a-1:events"
        once = namespaced("", key)
        twice = namespaced("", once)
        assert once == twice == key


# ---------------------------------------------------------------------------
# Integration with Settings (the field validator)
# ---------------------------------------------------------------------------


class TestSettingsIntegration:
    """The ``field_validator`` on ``RedisSettingsMixin``
    calls :func:`validate_prefix` once at boot.

    This test pins the integration: a malformed env var
    fails fast at ``Settings()`` construction, not on the
    first Redis write.
    """

    def test_empty_string_is_default(self) -> None:
        """Pre-076 behaviour: default value is empty."""
        from kntgraph.infra.config._base import BaseSettings

        class _TestSettings(BaseSettings):
            redis_key_prefix: str = ""

        settings = _TestSettings()
        assert settings.redis_key_prefix == ""
        # No raise -- the field_validator accepts "".

    def test_valid_prefix_round_trips(self) -> None:
        """A valid prefix survives Settings construction."""
        from kntgraph.infra.config._base import BaseSettings

        class _TestSettings(BaseSettings):
            redis_key_prefix: str = ""

        settings = _TestSettings(redis_key_prefix="acme-billing:")
        assert settings.redis_key_prefix == "acme-billing:"

    def test_invalid_prefix_fails_at_construction(self) -> None:
        """A malformed prefix raises during ``Settings()``.

        Operators see the error at process boot, not on
        the first Redis call. Uses the real
        ``RedisSettingsMixin`` so the field_validator is
        wired in (a hand-rolled subclass would bypass it
        and pass).
        """
        from kntgraph.infra.config._redis import RedisSettingsMixin

        class _TestSettings(RedisSettingsMixin):
            """Standalone ``Settings`` for the field_validator test."""

        with pytest.raises(ValueError, match="redis_key_prefix"):
            _TestSettings(redis_key_prefix="acme*")
