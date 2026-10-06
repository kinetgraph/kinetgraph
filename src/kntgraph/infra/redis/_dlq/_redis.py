# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
RedisDLQStorage — Redis impl of DLQStorage.

Iteration 5 (ADR-019). Owns the 4 Redis keys and the
hash-based idempotency protocol. The queue class
(``DeadLetterQueue``) consumes this storage and builds
``DeadLetterEvent`` domain objects.

Wire format
-----------

  - ``knt:dlq:events``           — Stream (one entry per DLQ row)
  - ``knt:dlq:by_event_id``      — Hash:
                                   ``<event_id>:<reason>`` → stream_id
                                   (with ``PLACEHOLDER`` marker during claim)
  - ``knt:dlq:by_agent``         — Hash: ``agent_id`` → first stream id
  - ``knt:dlq:reasons``          — Hash: ``reason`` → counter

Result contract (AGENTS.md §6): see ``DLQStorage``.

ADR-076 (namespace prefix)
-------------------------

The constants below are the canonical **suffix templates**.
At every read/write the adapter composes them with the
namespace prefix via :func:`infra.redis._prefix.namespaced`,
so two services on the same Redis do not cross-talk.
Empty ``key_prefix`` preserves the pre-076 wire format
byte-for-byte. ``ALL_KEYS`` returns the prefixed view
of all 4 keys (used by ``purge``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import structlog

from kntgraph.core.result import Err, Ok, Result

from .._client import RedisLike, safe_xrange
from .._codec import decode_dict, decode_value
from .._errors import MemoryError
from .._prefix import namespaced
from .._translation import translate_redis_call

logger = structlog.get_logger()


# Key suffix conventions. Centralised here so the queue
# does not need to know the wire convention. The adapter
# prepends the namespace prefix at every key build.
DLQ_STREAM_KEY = "knt:dlq:events"
DLQ_REASON_INDEX = "knt:dlq:reasons"
DLQ_AGENT_INDEX = "knt:dlq:by_agent"
DLQ_EVENT_INDEX = "knt:dlq:by_event_id"

# Idempotency placeholder. A concurrent writer holds this
# key while it appends; the next reader treats it as a
# concurrent-insert signal (raises ``IdempotencyConflict``
# in the legacy helper).
PLACEHOLDER = "PLACEHOLDER"

# Per-stream MAXLEN. The DLQ is bounded by retention policy;
# 1M entries is the default.
MAXLEN_DEFAULT = 1_000_000

# All 4 suffix templates, in dependency order. The adapter
# composes them with the prefix at runtime.
ALL_KEYS: tuple[str, ...] = (
    DLQ_STREAM_KEY,
    DLQ_AGENT_INDEX,
    DLQ_EVENT_INDEX,
    DLQ_REASON_INDEX,
)


def idem_key_for(event_id: str, reason: str) -> str:
    """Build the per-(event_id, reason) idempotency suffix."""
    return f"{event_id}:{reason}"


@dataclass(frozen=True)
class RedisDLQStorage:
    """Redis impl of :class:`DLQStorage`.

    ``key_prefix`` is the namespace prefix (ADR-076); see
    :mod:`infra.redis._prefix`. The composition rule lives
    in :meth:`_k` and is the only place every key is built.
    """

    client: RedisLike
    maxlen: int = MAXLEN_DEFAULT
    key_prefix: str = ""

    def _k(self, suffix: str) -> str:
        """Compose a namespaced Redis key from a suffix template."""
        return namespaced(self.key_prefix, suffix)

    def _all_keys(self) -> tuple[str, ...]:
        """Return the prefixed view of :data:`ALL_KEYS`.

        Used by :meth:`purge` to wipe every DLQ key the
        adapter owns. The order matches ``ALL_KEYS``
        (dependency order: stream first, then the index
        hashes that reference it).
        """
        return tuple(self._k(suffix) for suffix in ALL_KEYS)

    async def append(
        self,
        idem_key: str,
        payload: Mapping[str, str],
    ) -> Result[str, MemoryError]:
        """Append a DLQ entry idempotently on ``idem_key``.

        Wire steps:

          1. ``XADD <stream> MAXLEN <maxlen>`` — append the
             stream entry.
          2. ``HSET <event_index> PLACEHOLDER NX`` — claim
             the per-event_id slot (concurrent-insert signal).
          3. Replace placeholder with the stream id via
             ``HSET <event_index> <stream_id>``.

        If the ``HSETNX`` at step 2 fails (placeholder
        already present), a concurrent insert is in flight;
        we return ``Ok(stream_id)`` because the original
        write is still in progress and will complete. The
        idempotency contract: same ``idem_key`` → same
        stream id (the caller's dedup boundary).
        """
        try:
            # Check if already exists (sequential idempotency)
            existing = await self.client.hget(self._k(DLQ_EVENT_INDEX), idem_key)
            if existing is not None:
                return Ok(
                    existing.decode("utf-8")
                    if isinstance(existing, (bytes, bytearray))
                    else str(existing)
                )

            stream_id_bytes = await self.client.xadd(
                self._k(DLQ_STREAM_KEY),
                dict(payload),
                maxlen=self.maxlen,
            )
            # Two-phase claim: placeholder → final id.
            success = await self.client.hsetnx(
                self._k(DLQ_EVENT_INDEX), idem_key, PLACEHOLDER
            )
            stream_id = (
                stream_id_bytes.decode("utf-8")
                if isinstance(stream_id_bytes, (bytes, bytearray))
                else str(stream_id_bytes)
            )
            if not success:
                # Concurrent insert won the race to set placeholder/final id.
                # Retrieve the winner's stream_id.
                val = await self.client.hget(self._k(DLQ_EVENT_INDEX), idem_key)
                if val is not None:
                    return Ok(
                        val.decode("utf-8")
                        if isinstance(val, (bytes, bytearray))
                        else str(val)
                    )
                return Ok(PLACEHOLDER)

            await self.client.hset(self._k(DLQ_EVENT_INDEX), idem_key, stream_id)
            return Ok(stream_id)
        except Exception as e:  # noqa: BLE001
            # The body above issues 4 sequential Redis calls
            # (hget / xadd / hsetnx / hset) and is not easily
            # extracted to a single ``await`` for the
            # translate helper. The catch is left as a wide
            # ``except Exception`` (deferred to a future
            # refactor that splits ``append`` into
            # ``_check_existing`` / ``_xadd`` / ``_claim_placeholder`` /
            # ``_finalise_id`` per-step helpers, each of which
            # would route through ``translate_redis_call``).
            # Tracked in DEBT §2.39 as the "DLQ append 3-step
            # split" follow-up.
            logger.warning(
                "dlq_storage.append.redis_error",
                idem_key=idem_key,
                error=str(e),
            )
            return Err(MemoryError(f"redis error: {e}"))

    async def read(
        self, stream_id: str
    ) -> Result[Mapping[str, str] | None, MemoryError]:
        """Read a single DLQ entry by stream id.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            safe_xrange(
                self.client, self._k(DLQ_STREAM_KEY), min=stream_id, max=stream_id
            ),
            op_name="dlq_storage.read",
            error_cls=MemoryError,
            key=self._k(DLQ_STREAM_KEY),
            stream_id=stream_id,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        messages = result.ok_value()
        if not messages:
            return Ok(None)
        _, m = messages[0]
        return Ok(decode_dict(m))

    async def list_for_agent(
        self, agent_id: str, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries for one agent (forward-scan from head).

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.hget(self._k(DLQ_AGENT_INDEX), agent_id),
            op_name="dlq_storage.list_for_agent",
            error_cls=MemoryError,
            key=self._k(DLQ_AGENT_INDEX),
            agent_id=agent_id,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        head = decode_value(result.ok_value())
        if head is None:
            return Ok([])
        return await self._scan_from(head, count)

    async def list_by_reason(
        self, reason: str, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries with a given reason (full scan)."""
        result = await self.list_all(count)
        if result.is_err():
            return result
        messages: list[Mapping[str, str]] | None = result.ok_value()
        if messages is None:
            return Ok([])
        return Ok([m for m in messages if m.get("reason") == reason])

    async def list_all(
        self, count: int = 100
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """List DLQ entries (full scan).

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            safe_xrange(
                self.client, self._k(DLQ_STREAM_KEY), min="-", max="+", count=count
            ),
            op_name="dlq_storage.list_all",
            error_cls=MemoryError,
            key=self._k(DLQ_STREAM_KEY),
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        messages = result.ok_value() or []
        return Ok([decode_dict(m) for _, m in messages])

    async def read_index(
        self, event_id: str, reason: str
    ) -> Result[str | None, MemoryError]:
        """Look up the stream id for ``(event_id, reason)``.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.hget(self._k(DLQ_EVENT_INDEX), idem_key_for(event_id, reason)),
            op_name="dlq_storage.read_index",
            error_cls=MemoryError,
            key=self._k(DLQ_EVENT_INDEX),
            event_id=event_id,
            reason=reason,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(decode_value(result.ok_value()))

    async def find_by_event_id(self, event_id: str) -> Result[str | None, MemoryError]:
        """Find the first stream id for ``event_id`` across all reasons.

        Scans ``<event_id>:*`` keys of the per-event_id index.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        A Redis transport failure surfaces as
        ``Err(MemoryError(...))`` — the caller is the DLQ
        facade which already handles the Err and surfaces
        it through the public ``Result`` channel.
        """

        async def _scan() -> str | None:
            async for _, stream_id in self.client.hscan_iter(
                self._k(DLQ_EVENT_INDEX), match=f"{event_id}:*"
            ):
                decoded = decode_value(stream_id)
                if decoded is None or decoded == PLACEHOLDER:
                    continue
                return decoded
            return None

        result = await translate_redis_call(
            _scan(),
            op_name="dlq_storage.find_by_event_id",
            error_cls=MemoryError,
            key=self._k(DLQ_EVENT_INDEX),
            event_id=event_id,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(result.ok_value())

    async def bump_reason_counter(
        self, reason: str, delta: int
    ) -> Result[None, MemoryError]:
        """HINCRBY the per-reason counter by ``delta``.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            self.client.hincrby(self._k(DLQ_REASON_INDEX), reason, delta),
            op_name="dlq_storage.bump_reason_counter",
            error_cls=MemoryError,
            key=self._k(DLQ_REASON_INDEX),
            reason=reason,
            delta=delta,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)

    async def get_stats(self) -> Result[dict, MemoryError]:
        """Aggregate stats: total events, unique agents, by-reason.

        The inner ``XINFO STREAM`` call is a fail-soft
        catch (``XINFO`` raises on a missing stream —
        treat that as 0 entries). The outer wrap is
        :func:`translate_redis_call` per ADR-077.
        """
        from .._translation import translate_redis_call_fall_back

        async def _read_length() -> int:
            info = await self.client.xinfo_stream(self._k(DLQ_STREAM_KEY))
            if not isinstance(info, dict):
                return 0
            try:
                return int(info.get("length", 0))
            except (TypeError, ValueError):
                return 0

        length = await translate_redis_call_fall_back(
            _read_length(),
            op_name="dlq_storage.get_stats.length",
            fallback=0,
            key=self._k(DLQ_STREAM_KEY),
        )

        async def _read_reasons_and_agents() -> dict:
            reasons_raw = await self.client.hgetall(self._k(DLQ_REASON_INDEX))
            reasons = _decode_int_dict(reasons_raw)
            agents_count = await self.client.hlen(self._k(DLQ_AGENT_INDEX))
            return {
                "total_events": length,
                "unique_agents": agents_count,
                "by_reason": reasons,
            }

        result = await translate_redis_call(
            _read_reasons_and_agents(),
            op_name="dlq_storage.get_stats",
            error_cls=MemoryError,
            key=self._k(DLQ_REASON_INDEX),
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(result.ok_value() or {})

    async def purge(self) -> Result[int, MemoryError]:
        """Wipe all 4 DLQ keys. Returns the number of entries purged.

        The inner ``XINFO STREAM`` is a fail-soft catch
        (XINFO raises on a missing stream — treat as 0).
        The outer wrap is
        :func:`translate_redis_call` per ADR-077.
        """
        from .._translation import translate_redis_call_fall_back

        async def _read_length() -> int:
            info = await self.client.xinfo_stream(self._k(DLQ_STREAM_KEY))
            if not isinstance(info, dict):
                return 0
            try:
                return int(info.get("length", 0))
            except (TypeError, ValueError):
                return 0

        length = await translate_redis_call_fall_back(
            _read_length(),
            op_name="dlq_storage.purge.length",
            fallback=0,
            key=self._k(DLQ_STREAM_KEY),
        )
        result = await translate_redis_call(
            self.client.delete(*self._all_keys()),
            op_name="dlq_storage.purge",
            error_cls=MemoryError,
            key="<dlq-all-keys>",
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(length)

    async def drop_entry(
        self,
        event_id: str,
        reason: str,
        stream_id: str,
    ) -> Result[None, MemoryError]:
        """Remove a single DLQ entry.

        Skips XDEL when ``stream_id == PLACEHOLDER`` (the
        in-flight marker from a concurrent claim). The
        caller passes the stream_id (looked up via
        ``read_index``) so we avoid a second HGET.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """

        async def _do_drop() -> None:
            if stream_id and stream_id != PLACEHOLDER:
                await self.client.xdel(self._k(DLQ_STREAM_KEY), stream_id)
            await self.client.hdel(
                self._k(DLQ_EVENT_INDEX), idem_key_for(event_id, reason)
            )

        result = await translate_redis_call(
            _do_drop(),
            op_name="dlq_storage.drop_entry",
            error_cls=MemoryError,
            key=self._k(DLQ_EVENT_INDEX),
            event_id=event_id,
            reason=reason,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        return Ok(None)

    async def _scan_from(
        self, head_stream_id: str, count: int
    ) -> Result[list[Mapping[str, str]], MemoryError]:
        """Forward-scan the stream from a given head.

        Per ADR-077: the catch is centralised in
        :func:`kntgraph.infra.redis._translation.translate_redis_call`.
        """
        result = await translate_redis_call(
            safe_xrange(
                self.client,
                self._k(DLQ_STREAM_KEY),
                min=head_stream_id,
                max="+",
                count=count,
            ),
            op_name="dlq_storage._scan_from",
            error_cls=MemoryError,
            key=self._k(DLQ_STREAM_KEY),
            head=head_stream_id,
        )
        if result.is_err():
            return Err(result.err_value_or_raise())
        messages = result.ok_value() or []
        return Ok([decode_dict(m) for _, m in messages])


def _decode_int_dict(raw: dict) -> dict[str, int]:
    """Decode a hash whose values are decimal-encoded ints."""
    out: dict[str, int] = {}
    for k, v in raw.items():
        key = decode_value(k)
        val_str = decode_value(v)
        if key is None or val_str is None:
            continue
        try:
            out[key] = int(val_str)
        except (TypeError, ValueError):
            continue
    return out


__all__ = [
    "ALL_KEYS",
    "DLQ_AGENT_INDEX",
    "DLQ_EVENT_INDEX",
    "DLQ_REASON_INDEX",
    "DLQ_STREAM_KEY",
    "MAXLEN_DEFAULT",
    "PLACEHOLDER",
    "RedisDLQStorage",
    "idem_key_for",
]
