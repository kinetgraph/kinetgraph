# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Errors raised by the Redis adapter layer.

The framework uses these typed errors for ``Result`` mapping
in adapters. Callers should not catch ``Exception`` when
``RedisUnavailableError`` is enough.
"""


class RedisAdapterError(Exception):
    """Base for all Redis adapter errors."""


class RedisUnavailableError(RedisAdapterError):
    """Connection lost, timeout, or pool exhausted."""


class IdempotencyConflict(RedisAdapterError):
    """A concurrent writer holds the placeholder for this key.

    Callers map this to ``Err(PersistenceError(...))`` —
    the user-visible contract is "the write was not lost and
    not duplicated; retry later".
    """

    def __init__(self, idem_key: str) -> None:
        super().__init__(f"Concurrent insert in flight for {idem_key!r}")
        self.idem_key = idem_key


class MemoryError(RedisAdapterError):
    """Base for short-memory cache errors.

    ``ShortMemoryStorage.get_record`` /
    ``put_record`` / ``delete_record`` return
    ``Err(MemoryError(...))`` on Redis-side failures (per
    AGENTS.md §6: fail-closed, typed errors).
    """

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


class MemoryDecodeError(MemoryError):
    """The cached payload was malformed (corrupt JSON, etc.)."""


class MemorySerializationError(MemoryError):
    """The record could not be serialized to the cache wire format."""


class MemoryMiss(MemoryError):
    """The key was not present in the cache.

    Distinct from ``MemoryError`` (Redis-side failure) and
    ``MemoryDecodeError`` (corrupt payload). The three
    cases are modelled as separate error types so callers
    can dispatch on them without checking ``is None`` on
    the success channel of a ``Result``.

    Pattern::

        result = await storage.get_record(key)
        if result.is_ok():
            record = result.ok_value()       # hit
        elif isinstance(result.err_value(), MemoryMiss):
            pass                              # cache miss
        elif isinstance(result.err_value(), MemoryDecodeError):
            ...                              # corrupt payload
        else:
            ...                              # Redis down

    This replaces the older ``Ok(None) on miss`` shape
    (AGENTS.md §6.2: "tipos concretos, fail-closed").
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"cache miss for key={key!r}", key=key)


__all__ = [
    "IdempotencyConflict",
    "MemoryDecodeError",
    "MemoryError",
    "MemoryMiss",
    "MemorySerializationError",
    "RedisAdapterError",
    "RedisUnavailableError",
]
