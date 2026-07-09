# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
continuity.pii -- The PII gate for continuity.entity_seen.

ADR-014 §2.7: ``continuity.entity_seen`` MUST carry a
hash of the entity value, never the raw value. The
``value_hash`` field on the event is expected to be a
sha256-truncated fingerprint (16 chars, ``"sha256:"``
prefix), produced by
``ContinuityManager.hash_value``.

This module centralises the rule in one place.

Helpers:

  - `is_pii_hash(value)`: cheap predicate used by
    debug paths.

  - `check_pii_hash(value)`: Railway-style validation.
    Returns ``Ok(None)`` when the value is a valid
    PII hash, or ``Err(PersistenceError(...))`` with
    a message that names the expected format so
    operators can grep for misconfigured callers.

The module does NOT compute the hash itself (that
lives on ``ContinuityManager.hash_value`` which uses
``infra.hashing.short_hash``). It only enforces the
*contract* that the hash looks like one we can trust.
"""

from __future__ import annotations

from ...core.result import Err, Ok, PersistenceError, Result


PII_HASH_PREFIX = "sha256:"


def is_pii_hash(value: str) -> bool:
    """
    True when ``value`` looks like a sha256-truncated
    fingerprint (the format produced by
    ``ContinuityManager.hash_value``). Used by debug
    paths.
    """
    return isinstance(value, str) and value.startswith(PII_HASH_PREFIX)


def check_pii_hash(value: str) -> Result[None, PersistenceError]:
    """
    Validate that ``value`` is a PII hash. Returns
    ``Ok(None)`` when it is, or
    ``Err(PersistenceError(...))`` with a message that
    names the expected format.

    The check is deliberately strict: a non-string or
    a string without the ``sha256:`` prefix is a bug
    in the caller (forgot to hash, or used a different
    algorithm). We do NOT silently accept — the goal
    is to surface the bug at the boundary, not to make
    the gate leak.
    """
    if not is_pii_hash(value):
        return Err(
            PersistenceError(
                "entity value_hash must be a sha256:... fingerprint; "
                "raw values are not accepted by record_entity_seen"
            )
        )
    return Ok(None)


__all__ = ["PII_HASH_PREFIX", "check_pii_hash", "is_pii_hash"]
