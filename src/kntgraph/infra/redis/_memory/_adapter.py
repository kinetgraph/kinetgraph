# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
ShortMemoryStorage — domain-level Protocol for the three
short-term memory tiers (ADR-014).

Three tiers share the same pattern:

  - ``SessionManager``    — JSON cache, single-part identity.
  - ``ProfileManager``    — Hash cache, two-part identity.
  - ``ContinuityManager`` — Hash cache, sliding TTL.

The Protocol abstracts the storage-format-agnostic surface:
``get_record``, ``put_record``, ``delete_record``, ``iter_keys``.
The concrete Redis impls pick the right primitive (SET vs
HSET, JSON vs Hash) per tier.

Why "Short" prefix
------------------

The name ``MemoryStorage`` is too generic and could collide
with future caches (Knowledge, Solution, Solution-tier). The
"Short" prefix signals that this is the RAB-flavoured
short-memory contract (per ADR-014), bounded by TTL or
sliding-TTL semantics. Long-memory / archive caches would
get a different Protocol (e.g. ``ArchiveStorage``).

Naming
------

The interface uses storage-format-agnostic verbs (``record``)
to avoid coupling callers to JSON or Hash. The previous
``get_json`` / ``set_json`` implied a wire format; ``get_record``
does not.

Result contract
---------------

Per AGENTS.md §6 (fail-closed, typed errors), all mutating
operations return ``Result[T, MemoryError]``:

  - ``get_record``  returns ``Ok(mapping)`` on hit, ``Err``
    on miss (``MemoryMiss``) or Redis failure
    (``MemoryError`` / ``MemoryDecodeError``). Hit and
    miss are modelled as **distinct error types**, NOT
    as ``Ok(None)``, so callers can dispatch with
    ``isinstance`` instead of ``is None`` checks.
  - ``put_record``  returns ``Ok(None)`` on success,
    ``Err(MemoryError)`` on serialization or Redis failure.
  - ``delete_record`` returns ``Ok(None)`` on success,
    ``Err(MemoryError)`` on Redis failure.
  - ``iter_keys``    returns ``AsyncIterator[str]``; an empty
    prefix match or empty Redis is normal, no Result needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Optional, Protocol, Union, runtime_checkable

from ....core._typing import JsonValue

from kntgraph.core.result import Result

from .._errors import MemoryError


# ``CacheRecord`` is the wire shape accepted by ``put_record``.
# Two flavours:
#   - ``Mapping[str, JsonValue]`` — Hash tier (Profile,
#     Continuity) feeds ``dict[str, str]`` directly to
#     ``HSET``; the Redis client coerces each value to bytes.
#   - ``str`` — JSON tier (Session) pre-serialises to a JSON
#     string before ``SET``. The string IS the wire payload.
# Modelling as ``Union`` keeps the two flavours in one
# Protocol while preventing ``Any`` (AGENTS.md §1).
CacheRecord = Union[str, Mapping[str, JsonValue]]


@runtime_checkable
class ShortMemoryStorage(Protocol):
    """Domain interface for the per-tier short-memory cache.

    Three tiers (Session, Profile, Continuity) plug concrete
    implementations; ``BaseShortTermMemory`` consumes the
    Protocol.
    """

    async def get_record(
        self, key: str
    ) -> Result[Mapping[str, JsonValue], MemoryError]:
        """Read a record by key.

        - ``Ok(mapping)`` on hit.
        - ``Err(MemoryMiss(key))`` on miss.
        - ``Err(MemoryDecodeError(...))`` on corrupt payload.
        - ``Err(MemoryError(...))`` on Redis-side failure.

        Callers MUST handle ``MemoryMiss`` separately (it
        is the read-through fallback signal, not an
        error to surface). The split between hit/miss/
        decode/io is intentional: each is a different
        recovery action (cache fill / log + continue /
        log + delete key / retry).
        """
        ...

    async def put_record(
        self,
        key: str,
        record: CacheRecord,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> Result[None, MemoryError]:
        """Persist a record. ``Ok(None)`` on success.

        ``record`` is either a JSON-encoded ``str`` (for
        the Session tier) or a Hash mapping ``Mapping[str,
        JsonValue]`` (for Profile/Continuity). The
        concrete impl picks the right Redis primitive.
        """
        ...

    async def delete_record(self, key: str) -> Result[None, MemoryError]:
        """Remove a record. Idempotent."""
        ...

    def iter_keys(self, prefix: str) -> AsyncIterator[str]:
        """Yield keys matching the prefix (used by ``list_for_tenant``)."""
        ...


__all__ = ["CacheRecord", "ShortMemoryStorage"]
