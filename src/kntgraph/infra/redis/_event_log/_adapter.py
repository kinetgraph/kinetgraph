# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
EventLogStorage — domain interface + Redis implementation.

The ``EventLog`` class in ``stream/event_log`` is a thin
orchestrator: validation, tenant ownership, signature, and
Result mapping. The actual Redis I/O lives here.

Why split
---------

  - ``EventLogStorage`` (Protocol) lets tests inject a
    fake storage without mocking ``redis.asyncio``.
  - ``RedisEventLogAdapter`` owns the wire format: codec,
    MAXLEN, idempotency keys. The rest of the framework
    does not need to know.
  - The adapter returns ``Event`` already parsed, so the
    codec does not leak back into ``EventLog``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from kntgraph.core.event import Event
from kntgraph.core.result import Err, Ok, PersistenceError, Result

from .._client import RedisLike
from .._errors import IdempotencyConflict
from . import _idempotency
from ._keys import (
    MAXLEN_DEFAULT,
    SCAN_PATTERN,
    event_id_key,
    parse_agent_id_from_stream_key,
    stream_key_for_agent,
)


def _event_to_redis(event: Event) -> dict[str, str]:
    """Local import wrapper — see module docstring."""
    from kntgraph.stream.event_log.codec import event_to_redis

    return event_to_redis(event)


def _parse_event(mid: bytes | str, mdata: dict) -> Event:
    """Local import wrapper — see module docstring."""
    from kntgraph.stream.event_log.codec import parse_event

    from typing import cast

    return parse_event(cast(bytes, mid), mdata)


if TYPE_CHECKING:
    pass


logger = structlog.get_logger()


class EventLogStorage(Protocol):
    """Domain interface for the per-agent EventLog."""

    async def append(
        self, *, agent_id: str, event: Event
    ) -> Result[str, PersistenceError]: ...

    async def read(
        self,
        agent_id: str,
        *,
        start: str = "-",
        end: str = "+",
        count: int | None = None,
    ) -> list[Event]: ...

    async def read_with_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]: ...

    async def read_latest(self, agent_id: str, n: int = 1) -> list[Event]: ...

    async def stream_len(self, agent_id: str) -> int: ...

    async def list_agents(self) -> list[str]: ...

    async def delete(self, agent_id: str) -> None: ...


@dataclass(frozen=True)
class RedisEventLogAdapter:
    """Redis implementation of :class:`EventLogStorage`.

    Owns the wire format (codec, MAXLEN, idempotency, key
    conventions). The ``EventLog`` class above this adapter
    is responsible only for validation, signature, resilience
    and the public ``Result`` contract.
    """

    client: RedisLike
    maxlen: int = MAXLEN_DEFAULT

    async def append(
        self, *, agent_id: str, event: Event
    ) -> Result[str, PersistenceError]:
        try:
            # Use module attribute lookup so tests can patch
            # ``_idempotency.claim_event_id_slot`` and have
            # the patch apply here.
            stream_id = await _idempotency.claim_event_id_slot(
                redis=self.client,
                idem_key=event_id_key(str(event.event_id)),
                stream_key=stream_key_for_agent(agent_id),
                payload=_event_to_redis(event),
                maxlen=self.maxlen,
            )
        except IdempotencyConflict:
            logger.debug(
                "event_log.append.idempotent_conflict",
                event_id=str(event.event_id),
                agent_id=agent_id,
            )
            return Err(PersistenceError("Concurrent insert in flight"))
        except Exception as e:
            logger.error(
                "event_log.append.error",
                event_id=str(event.event_id),
                agent_id=agent_id,
                error=str(e),
            )
            return Err(PersistenceError(f"Redis error: {e}"))
        logger.debug(
            "event_log.append.ok",
            event_id=str(event.event_id),
            agent_id=agent_id,
            stream_id=stream_id,
        )
        return Ok(stream_id)

    async def read(
        self,
        agent_id: str,
        *,
        start: str = "-",
        end: str = "+",
        count: int | None = None,
    ) -> list[Event]:
        kwargs: dict = {"min": start, "max": end}
        if count is not None:
            kwargs["count"] = count
        messages = await self.client.xrange(stream_key_for_agent(agent_id), **kwargs)
        return [_parse_event(mid, mdata) for mid, mdata in messages]

    async def read_with_cursor(
        self, agent_id: str, cursor: str
    ) -> tuple[list[Event], str]:
        if cursor == "-" or cursor == "0-0":
            start = "-"
        else:
            start = f"({cursor}"

        messages = await self.client.xrange(
            stream_key_for_agent(agent_id), min=start, max="+"
        )

        if not messages:
            return [], cursor

        events = [_parse_event(mid, mdata) for mid, mdata in messages]

        last_stream_id = messages[-1][0]
        if isinstance(last_stream_id, bytes):
            last_stream_id = last_stream_id.decode("utf-8")

        return events, last_stream_id

    async def read_latest(self, agent_id: str, n: int = 1) -> list[Event]:
        messages = await self.client.xrevrange(
            stream_key_for_agent(agent_id),
            min="-",
            max="+",
            count=n,
        )
        return [_parse_event(mid, mdata) for mid, mdata in messages]

    async def stream_len(self, agent_id: str) -> int:
        try:
            info = await self.client.xinfo_stream(stream_key_for_agent(agent_id))
        except self._response_error():
            return 0
        return int(info.get("length", 0))

    async def list_agents(self) -> list[str]:
        ids: list[str] = []
        async for key in self.client.scan_iter(match=SCAN_PATTERN, count=100):
            from .._codec import decode_value

            decoded = decode_value(key) or ""
            agent_id = parse_agent_id_from_stream_key(decoded)
            if agent_id is not None:
                ids.append(agent_id)
        return ids

    async def delete(self, agent_id: str) -> None:
        await self.client.delete(stream_key_for_agent(agent_id))

    @staticmethod
    def _response_error() -> type[Exception]:
        """Return the ResponseError type without importing at module top."""
        from redis.exceptions import ResponseError

        return ResponseError


__all__ = ["EventLogStorage", "RedisEventLogAdapter"]
