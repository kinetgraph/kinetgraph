# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
RedisAPIKeyStorage — Redis impl of APIKeyStorage.

Iteration 3 (ADR-019). Owns the Redis I/O and the key
suffix ``knt:api:keys:<digest>``. Does NOT decode the wire
format; the verifier does that.

Namespace prefix (ADR-076 / DEBT §2.35 follow-up #2)
---------------------------------------------------

``KEY_PREFIX`` is the **suffix template** the storage
appends to the operator-configurable namespace prefix
(``Settings.redis_key_prefix``). Two services sharing
one Redis with different ``KNT_REDIS_KEY_PREFIX`` values
now write to disjoint keyspaces: service A's binding
table lives at ``acme-billing:knt:api:keys:<digest>``;
service B's at ``crm:knt:api:keys:<digest>``. Empty
``key_prefix`` (the default) preserves the pre-076 wire
format byte-for-byte.

The legacy module-level :func:`storage_key` function is
preserved as a thin helper for the migration script and
external code; the canonical composition lives on the
storage instance.

Result contract (AGENTS.md §6): see ``APIKeyStorage``
docstring for the full contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike
from .._errors import MemoryError
from .._prefix import namespaced, validate_prefix


logger = structlog.get_logger()

# Key suffix template. Centralised here so the verifier
# does not need to know the wire convention. The storage
# composes this with the operator's ``redis_key_prefix``
# at every key build (ADR-076 §2.3). The trailing ``:`` is
# the boundary before the digest; the digest is appended
# directly (no separator colon between prefix and digest).
KEY_PREFIX: str = "knt:api:keys:"
"""Suffix template for the per-binding Redis key (``knt:api:keys:``
+ ``<digest>``). Composed with the namespace prefix at
construction time -- not the full key."""


def storage_key(prefix: str, digest: str) -> str:
    """Build the namespaced Redis key for a binding under
    ``prefix``.

    Empty ``prefix`` returns the unprefixed key
    (``"knt:api:keys:<digest>"``) -- byte-for-byte identical
    to the pre-ADR-076 wire format. Non-empty ``prefix``
    concatenates the namespace in front.

    The composition is a plain string concatenation -- no
    separator is inserted between the prefix and the
    ``knt:api:keys:`` suffix. Operators who want a
    trailing colon write ``"acme-billing:"`` (note the
    colon).
    """
    return namespaced(prefix, KEY_PREFIX + digest)


@dataclass(frozen=True)
class RedisAPIKeyStorage:
    """Redis impl of :class:`APIKeyStorage`.

    ``key_prefix`` (ADR-076) namespaces every Redis key
    the storage writes or reads. Empty ``key_prefix``
    (the default) is byte-for-byte identical to the
    pre-076 wire format.
    """

    client: RedisLike
    key_prefix: str = ""

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks attribute assignment
        # after construction, so we validate in
        # ``__post_init__`` (called once by the dataclass
        # machinery; ``validate_prefix`` is the same check
        # every other adapter runs at construction).
        validate_prefix(self.key_prefix)

    def storage_key(self, digest: str) -> str:
        """Compose the namespaced Redis key for ``digest``.

        Thin wrapper over the module-level :func:`storage_key`
        that uses ``self.key_prefix`` instead of taking it
        as an argument. The composition rule is identical.
        """
        return storage_key(self.key_prefix, digest)

    async def lookup(self, digest: str) -> Result[Optional[bytes], MemoryError]:
        """Look up a key binding by digest.

        Returns ``Ok(None)`` on miss; ``Err(MemoryError)`` on
        Redis failure. The raw bytes are returned untouched
        — the verifier owns the wire format decode.
        """
        try:
            raw = await self.client.get(self.storage_key(digest))
        except Exception as e:
            logger.warning(
                "api_key_storage.lookup.redis_error",
                digest=digest,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        if raw is None:
            return Ok(None)
        # ``decode_responses=False`` keeps raw bytes; if
        # the caller flipped it, accept str too.
        if isinstance(raw, (bytes, bytearray)):
            return Ok(bytes(raw))
        if isinstance(raw, str):
            return Ok(raw.encode("utf-8"))
        return Err(MemoryError(f"unexpected redis return type: {type(raw).__name__}"))

    async def store(self, digest: str, payload: bytes) -> Result[None, MemoryError]:
        """Persist a key binding (raw bytes)."""
        try:
            await self.client.set(self.storage_key(digest), payload)
        except Exception as e:
            logger.warning(
                "api_key_storage.store.redis_error",
                digest=digest,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)

    async def delete(self, digest: str) -> Result[None, MemoryError]:
        """Remove a key binding. Idempotent."""
        try:
            await self.client.delete(self.storage_key(digest))
        except Exception as e:
            logger.warning(
                "api_key_storage.delete.redis_error",
                digest=digest,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))
        return Ok(None)


__all__ = ["KEY_PREFIX", "RedisAPIKeyStorage", "storage_key"]
