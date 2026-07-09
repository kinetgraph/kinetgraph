# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
APIKeyStorage — domain Protocol for API key bindings.

Iteration 3 (ADR-019). The storage layer is bytes-in-bytes-out
with no JSON encoding: the verifier (``RedisAPIKeyVerifier``)
owns the JSON parse + Principal construction. The storage
just owns the Redis I/O and the key convention.

Why split
---------

The ``RedisAPIKeyVerifier.verify`` (CC=7) mixes five concerns:

  1. Input validation (empty key → ``AuthError(missing)``)
  2. SHA-256 hashing (key → digest)
  3. Redis I/O (digest lookup)
  4. Wire format decode (JSON or legacy string)
  5. Principal construction

Iteration 3 moves (3) and (4)'s decoding of bytes→str to the
storage. The verifier stays thin: hash → ``storage.lookup`` →
parse → Principal. The CC of the verifier drops (4 is now a
4-line function); the storage is testable in isolation.

Naming
------

``lookup`` returns raw ``bytes | None``. The storage does
NOT know the wire format (JSON vs legacy string). The
verifier decides how to interpret the bytes.

Result contract (AGENTS.md §6):

  - ``lookup``  returns ``Ok(raw_bytes)`` / ``Ok(None)`` /
    ``Err(MemoryError)``.
  - ``store``   returns ``Ok(None)`` / ``Err(MemoryError)``.
  - ``delete``  returns ``Ok(None)`` / ``Err(MemoryError)``.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from kntgraph.core.result import Result

from .._errors import MemoryError


@runtime_checkable
class APIKeyStorage(Protocol):
    """Domain interface for API key bindings (auth layer)."""

    async def lookup(self, digest: str) -> Result[Optional[bytes], MemoryError]:
        """Look up a key binding by sha256 digest.

        Returns ``Ok(raw_bytes)`` on hit, ``Ok(None)`` on
        miss, ``Err(MemoryError)`` on Redis failure.
        """
        ...

    async def store(self, digest: str, payload: bytes) -> Result[None, MemoryError]:
        """Persist a key binding (raw bytes; verifier owns encoding)."""
        ...

    async def delete(self, digest: str) -> Result[None, MemoryError]:
        """Remove a key binding. Idempotent."""
        ...


__all__ = ["APIKeyStorage"]
