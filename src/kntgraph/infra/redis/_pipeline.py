# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``PipelineLike`` — framework-level Protocol for the transactional
pipeline returned by ``client.pipeline()``.

Split out of :mod:`kntgraph.infra.redis._client` so the client
Protocol and the pipeline Protocol can evolve independently: the
client's surface is driven by the framework's Redis commands, the
pipeline's by the atomic multi-key write paths.

The pipeline backs the EventLog idempotency claim and the
short-memory writers (session, profile, continuity, solution) —
every write that must land atomically across two keys.
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
