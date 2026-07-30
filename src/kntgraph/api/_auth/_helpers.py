# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
api._auth._helpers -- pure helpers for the API key
verifier pipeline.

These helpers are intentionally module-level (not
methods) so they stay CC = 1-2 and don't pollute the
:class:`RedisAPIKeyVerifier` body. They are stateless
and have no I/O.

This module is a private implementation detail of
``_auth``; the public surface is unchanged.

The pre-ADR-017 ``_legacy_principal`` factory was
removed in 0.10.0 (ADR-017 §7.3). The plain-string
fallback in ``RedisAPIKeyVerifier`` is now
``AuthError(kind="malformed")``; deployments with
legacy bindings must run
``scripts/migrate_principals.py --apply`` before
upgrading.
"""

from __future__ import annotations

import hashlib


def _digest(api_key: str) -> str:
    """SHA-256 hex digest of the API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _decode(raw: bytes) -> str:
    """Decode raw bytes (or str, for safety) into a UTF-8 string."""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8")
    return str(raw)


__all__ = ["_decode", "_digest"]
