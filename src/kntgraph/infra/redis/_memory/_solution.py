# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisSolutionStore -- Hash-backed Solution tier cache (ADR-010 / ADR-049).

The Solution tier's read-side cache is keyed by
``(tool_name, params_fingerprint)``. Each tool has its own
Redis Hash under ``knt:solution:<tool_name>``; the Hash
field is the ``params_fingerprint`` and the value is a
JSON-encoded :class:`CachedSolution` payload.

Why a Hash per tool (instead of one big Hash or a single
key per Solution):

  - **Bounded scan**: a single ``HGET`` is O(1); the
    lookup does not scan the whole store. The
    ``InMemorySolutionStore`` uses a flat
    ``dict[(tool, fp), Solution]`` for the same reason.
  - **Clean isolation**: operator-side tooling that
    inspects one tool's cache (e.g. ``HGETALL
    knt:solution:knowledge_lookup``) sees exactly that
    tool's Solutions without filtering.
  - **TTL scoping**: the per-tool Hash can carry a
    different TTL (or no TTL at all) per operator
    preference. The shipped default is no TTL (Solutions
    are explicitly invalidated by the operator, not by a
    timer) but ``ttl_seconds`` is wired for callers that
    want it.

Wire format
-----------

For ``CachedSolution(...)`` with
``tool_name="knowledge_lookup"`` and
``params_fingerprint="<fp>"``:

    Key:    knt:solution:knowledge_lookup
    Field:  <fp>
    Value:  JSON payload, e.g.::

        {
          "tool_name": "knowledge_lookup",
          "params_fingerprint": "<fp>",
          "confidence": 5,
          "result": {"answer": "..."},
          "source_completion_event_id": "..."
        }

Result contract (AGENTS.md §6)
------------------------------

- :meth:`find_match` returns ``Ok(CachedSolution)`` on
  hit; ``Ok(None)`` on miss; ``Err(SolutionStoreError)``
  on Redis-side failure or corrupt payload. The lookup
  system (``SolutionLookupSystem``) treats
  ``Err(SolutionStoreError)`` as a miss for stats
  purposes (the LLM path takes over), so transient
  Redis errors do not stall the chat loop.
- :meth:`put` returns ``Ok(None)`` on success;
  ``Err(SolutionStoreError)`` on Redis-side failure or
  serialization error.
- :meth:`delete` returns ``Ok(None)`` regardless of
  whether the key existed (idempotent).

The Protocol ``SolutionStoreLike`` (``agents.memory.solution_lookup``)
treats ``find_match`` as the only required method; the
:class:`RedisSolutionStore` exposes the wider
``put`` / ``delete`` / ``iter_keys`` API that operators
need to populate and audit the cache, but they are
optional from the read-side system's perspective.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from kntgraph.agents.memory.solution_lookup import CachedSolution
from kntgraph.core.result import Err, Ok, Result
from kntgraph.infra.redis._client import RedisLike
from kntgraph.infra.redis._codec import decode_dict, decode_value


logger = structlog.get_logger()


SOLUTION_KEY_PREFIX = "knt:solution:"


class SolutionStoreError(Exception):
    """Base for Redis Solution store errors.

    Mapped to ``Err(SolutionStoreError(...))`` at the
    adapter boundary (per AGENTS.md §6: typed errors,
    fail-closed). The read-side ``SolutionLookupSystem``
    treats the error as a miss so the LLM fallback takes
    over -- a Redis outage MUST NOT stall the chat loop.
    """

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        params_fingerprint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.params_fingerprint = params_fingerprint


class SolutionStoreDecodeError(SolutionStoreError):
    """The cached payload was malformed (corrupt JSON, missing fields)."""


class SolutionStoreSerializationError(SolutionStoreError):
    """The :class:`CachedSolution` could not be JSON-encoded."""


@dataclass(frozen=True)
class RedisSolutionStore:
    """Hash-backed Solution cache implementing :class:`SolutionStoreLike`.

    Wire layout: one Redis Hash per tool (``knt:solution:<tool_name>``);
    field = ``params_fingerprint``; value = JSON
    :class:`CachedSolution`.

    Concurrency: ``put`` uses a transactional pipeline
    (``DELETE`` + ``HSET`` + ``EXPIRE``) so the TTL is
    always applied atomically with the value (no window
    where the entry has no TTL).

    Idempotency: ``put`` is last-write-wins (the Solution
    promoter's own concurrency check, ADR-010 §3.4, is
    upstream of this adapter; we are the durable sink).
    """

    client: RedisLike
    ttl_seconds: Optional[int] = None

    @staticmethod
    def _key(tool_name: str) -> str:
        """Build the Redis key for a tool's Solution Hash."""
        return f"{SOLUTION_KEY_PREFIX}{tool_name}"

    @staticmethod
    def _decode_payload(
        raw: Any,
        *,
        tool_name: str,
        params_fingerprint: str,
    ) -> CachedSolution:
        """Decode a Redis Hash value into a :class:`CachedSolution`.

        Raises :class:`SolutionStoreDecodeError` on
        corrupt JSON or missing required fields.
        """
        text = decode_value(raw)
        if text is None:
            raise SolutionStoreDecodeError(
                "empty payload",
                tool_name=tool_name,
                params_fingerprint=params_fingerprint,
            )
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            raise SolutionStoreDecodeError(
                f"invalid JSON: {e}",
                tool_name=tool_name,
                params_fingerprint=params_fingerprint,
            ) from e
        if not isinstance(data, dict):
            raise SolutionStoreDecodeError(
                f"payload is not a mapping: {type(data).__name__}",
                tool_name=tool_name,
                params_fingerprint=params_fingerprint,
            )
        try:
            return CachedSolution(
                tool_name=str(data.get("tool_name", tool_name)),
                params_fingerprint=str(
                    data.get("params_fingerprint", params_fingerprint)
                ),
                confidence=int(data.get("confidence", 0)),
                result=dict(data.get("result", {})),
                source_completion_event_id=str(
                    data.get("source_completion_event_id", "")
                ),
            )
        except (TypeError, ValueError) as e:
            raise SolutionStoreDecodeError(
                f"missing or invalid fields: {e}",
                tool_name=tool_name,
                params_fingerprint=params_fingerprint,
            ) from e

    async def find_match(
        self,
        *,
        tool_name: str,
        params_fingerprint: str,
        min_confidence: int,
    ) -> Optional[CachedSolution]:
        """Read-side API (the ``SolutionStoreLike`` contract).

        On hit: returns the cached Solution when
        ``confidence >= min_confidence``. On miss:
        returns ``None``. On Redis error or corrupt
        payload: returns ``None`` and logs the failure
        so the dispatcher's LLM fallback can take over
        (the read-side is fail-open by design -- see
        ADR-049 §2.1.3).
        """
        key = self._key(tool_name)
        try:
            raw = await self.client.hget(key, params_fingerprint)
        except Exception as e:
            logger.warning(
                "solution_store.find_match.redis_error",
                tool_name=tool_name,
                params_fingerprint=params_fingerprint,
                error=str(e),
            )
            return None
        text = decode_value(raw)
        if text is None:
            return None
        try:
            solution = self._decode_payload(
                text,
                tool_name=tool_name,
                params_fingerprint=params_fingerprint,
            )
        except SolutionStoreDecodeError as e:
            logger.warning(
                "solution_store.find_match.decode_error",
                tool_name=tool_name,
                params_fingerprint=params_fingerprint,
                error=str(e),
            )
            return None
        if solution.confidence < min_confidence:
            # Below the operator's bar; the lookup system
            # treats this as a miss with the
            # ``bypass_low_confidence`` stat (separate
            # from a plain cache miss).
            return None
        return solution

    async def put(self, solution: CachedSolution) -> Result[None, SolutionStoreError]:
        """Write a Solution to the cache.

        Last-write-wins per ``(tool_name, params_fingerprint)``.
        The TTL is applied atomically with the value via a
        transactional pipeline (no window where the entry
        is TTL-less after a partial failure).
        """
        key = self._key(solution.tool_name)
        try:
            payload = json.dumps(
                {
                    "tool_name": solution.tool_name,
                    "params_fingerprint": solution.params_fingerprint,
                    "confidence": solution.confidence,
                    "result": dict(solution.result),
                    "source_completion_event_id": solution.source_completion_event_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError, RuntimeError) as e:
            logger.warning(
                "solution_store.put.serialization_error",
                tool_name=solution.tool_name,
                params_fingerprint=solution.params_fingerprint,
                error=str(e),
            )
            return Err(
                SolutionStoreSerializationError(
                    f"cannot serialize: {e}",
                    tool_name=solution.tool_name,
                    params_fingerprint=solution.params_fingerprint,
                )
            )
        try:
            pipe = self.client.pipeline(transaction=True)
            pipe.hset(key, solution.params_fingerprint, payload)
            if self.ttl_seconds is not None and self.ttl_seconds > 0:
                pipe.expire(key, self.ttl_seconds)
            await pipe.execute()
        except Exception as e:
            logger.warning(
                "solution_store.put.redis_error",
                tool_name=solution.tool_name,
                params_fingerprint=solution.params_fingerprint,
                error=str(e),
            )
            return Err(
                SolutionStoreError(
                    f"redis error: {e}",
                    tool_name=solution.tool_name,
                    params_fingerprint=solution.params_fingerprint,
                )
            )
        return Ok(None)

    async def delete(
        self,
        tool_name: str,
        params_fingerprint: str,
    ) -> Result[None, SolutionStoreError]:
        """Remove a single Solution. Idempotent: missing
        keys return ``Ok(None)``."""
        key = self._key(tool_name)
        try:
            await self.client.hdel(key, params_fingerprint)
        except Exception as e:
            logger.warning(
                "solution_store.delete.redis_error",
                tool_name=tool_name,
                params_fingerprint=params_fingerprint,
                error=str(e),
            )
            return Err(
                SolutionStoreError(
                    f"redis error: {e}",
                    tool_name=tool_name,
                    params_fingerprint=params_fingerprint,
                )
            )
        return Ok(None)

    async def iter_keys(self, tool_name: str) -> AsyncIterator[str]:
        """Yield every ``params_fingerprint`` cached for a
        tool. Useful for operator-side audit tooling; not
        used by the read-side lookup system.

        ``hscan_iter`` (per ``redis.asyncio.Redis``) yields
        ``(field, value)`` tuples; we discard the value
        because the fingerprint is the field.
        """
        name = self._key(tool_name)
        async for entry in self.client.hscan_iter(name, match=None, count=100):
            # ``hscan_iter`` yields either ``(field, value)``
            # tuples (the canonical Redis shape) or bare
            # ``field`` strings (some test doubles). The
            # runtime check makes the adapter tolerant to
            # both shapes; production Redis always returns
            # the tuple form.
            if isinstance(entry, tuple):
                field = entry[0]
            else:
                field = entry
            decoded = decode_value(field)
            if decoded:
                yield decoded

    async def read_all(self, tool_name: str) -> dict[str, CachedSolution]:
        """Read every Solution for a tool. Useful for
        operator-side inspection; returns a
        ``{params_fingerprint: CachedSolution}`` map.
        Corrupt entries are skipped and logged (the
        operator can decide whether to ``delete`` them).
        """
        key = self._key(tool_name)
        try:
            raw = await self.client.hgetall(key)
        except Exception as e:
            logger.warning(
                "solution_store.read_all.redis_error",
                tool_name=tool_name,
                error=str(e),
            )
            return {}
        decoded = decode_dict(raw)
        out: dict[str, CachedSolution] = {}
        for fp, text in decoded.items():
            try:
                out[fp] = self._decode_payload(
                    text, tool_name=tool_name, params_fingerprint=fp
                )
            except SolutionStoreDecodeError as e:
                logger.warning(
                    "solution_store.read_all.decode_error",
                    tool_name=tool_name,
                    params_fingerprint=fp,
                    error=str(e),
                )
                continue
        return out


__all__ = [
    "RedisSolutionStore",
    "SOLUTION_KEY_PREFIX",
    "SolutionStoreDecodeError",
    "SolutionStoreError",
    "SolutionStoreSerializationError",
]
