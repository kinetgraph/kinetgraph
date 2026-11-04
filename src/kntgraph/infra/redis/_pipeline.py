# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``PipelineLike`` — framework-level Protocol for the
pipeline returned by ``client.pipeline()``.

Split out of :mod:`kntgraph.infra.redis._client` so the client
Protocol and the pipeline Protocol can evolve independently: the
client's surface is driven by the framework's Redis commands, the
pipeline's by the write paths that touch more than one key.

The pipeline backs the EventLog idempotency claim, the
tool-queue dedup claim, the World checkpoint pair and the
short-memory writers (session, profile, continuity, solution).

Cluster mode
------------

Every pipeline in the framework is built with
``transaction=False``. A ``MULTI/EXEC`` is rejected by a
cluster server when its keys land in different hash slots
(``CROSSSLOT``), and the framework's keys carry no hash tag.
The atomicity the write paths need comes from their own
protocols (a ``SET ... NX`` claim, an ordered pair of writes),
not from the transaction.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PipelineLike(Protocol):
    """Subset of ``redis.asyncio.client.Pipeline`` used by
    the EventLog + the short-memory adapters.

    Add each new pipeline operation used by a call site here.
    """

    def xadd(
        self,
        name: str,
        fields: dict,
        maxlen: int | None = None,
    ) -> PipelineLike: ...

    def set(
        self,
        key: str,
        value: str | bytes,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> PipelineLike: ...

    def delete(self, *keys: str) -> PipelineLike: ...

    def unlink(self, *keys: str) -> PipelineLike: ...

    def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        mapping: dict | None = None,
    ) -> PipelineLike: ...

    def expire(self, key: str, seconds: int) -> PipelineLike: ...

    async def execute(self) -> list: ...


__all__ = ["PipelineLike"]
