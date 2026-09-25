# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
infra.redis._prefix -- Redis key namespace prefix (ADR-076).

The framework namespaces every Redis key under a single
configurable prefix so two services sharing one Redis
do not cross-talk. The prefix is empty by default (the
pre-ADR-076 behaviour, byte-for-byte); operators opt in
by setting ``KNT_REDIS_KEY_PREFIX`` to e.g. ``acme-billing:``.

This module is the single source of truth for the
prefix:

  - :func:`validate_prefix` -- constraint check.
    ``""`` is always valid; non-empty must match
    ``[a-zA-Z0-9_.:-]+`` (no glob / format-template
    syntax, no leading separator).
  - :func:`namespaced` -- compose the prefix with a
    suffix template. Empty prefix is the fast path
    that returns the suffix unchanged.

Composition rules:

  - The prefix is concatenated at the front of the key
    **without** an automatic separator. Operators who
    want a trailing colon write ``acme-billing:`` (note
    the colon).
  - The suffix templates are owned by the per-feature
    adapter modules (``infra.redis._event_log._keys``,
    ``infra.redis._memory._profile``, etc.). This module
    knows nothing about those keys; it only joins the
    prefix with whatever suffix the adapter passes.
"""

from __future__ import annotations

import re

# Allowed characters: alphanumerics, underscore, dot,
# dash, colon. Excludes ``*`` (would break SCAN glob
# patterns), ``{`` / ``}`` (would break ``.format``
# templates), and whitespace (would corrupt CLI parsing).
_PREFIX_RE = re.compile(r"^[a-zA-Z0-9_.:-]+$")


def validate_prefix(prefix: str) -> None:
    """Raise ``ValueError`` if ``prefix`` is not a valid
    Redis key namespace prefix.

    Rules:

      - Empty string is always valid (the default; no
        behaviour change vs pre-ADR-076).
      - Non-empty must match ``[a-zA-Z0-9_.:-]+``.
      - A bare ``":"`` is rejected (looks like an
        oversight; operators who want trailing colons
        write ``acme-billing:``).
      - Any character outside the allow-list raises
        (covers ``*``, ``{``, whitespace, unicode).

    The check is enforced once at ``Settings`` construction
    (via ``field_validator`` on ``redis_key_prefix``); the
    per-key-build path does NOT re-validate, since the
    prefix cannot change after boot.
    """
    if prefix == "":
        return
    if not isinstance(prefix, str):
        raise ValueError(f"redis_key_prefix must be str, got {type(prefix).__name__}")
    if not _PREFIX_RE.match(prefix):
        raise ValueError(
            f"redis_key_prefix contains invalid characters: {prefix!r}. "
            f"Allowed: alphanumerics, ':', '_', '.', '-'."
        )
    if prefix == ":":
        raise ValueError(
            "redis_key_prefix must not be just ':' "
            "(looks like an oversight; drop it or set a real namespace)."
        )


def namespaced(prefix: str, key: str) -> str:
    """Return ``key`` prefixed with ``prefix`` if non-empty.

    The composition is a plain string concatenation --
    no separator is inserted. Empty prefix is the fast
    path that returns ``key`` unchanged (matches the
    pre-ADR-076 wire format byte-for-byte).

    Args:
        prefix: the namespace prefix (already validated
            at boot; this function does not re-check).
        key: the per-feature suffix template the adapter
            built (``"knt:agents:{id}:events"``,
            ``"knt:dlq:events"``, etc.).

    Returns:
        The composed Redis key. For ``prefix=""`` and
        ``key="knt:agents:a-1:events"`` the result is
        exactly ``"knt:agents:a-1:events"``.
    """
    if not prefix:
        return key
    return f"{prefix}{key}"


__all__ = ["namespaced", "validate_prefix"]
