# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Test doubles for ``events/dlq`` — fake at the adapter
level (SUT pattern), not mocks at the client level.

Why a fake rather than ``monkeypatch.setattr`` (the
existing test pattern in ``test_dlq_unit.py``):

- ``monkeypatch`` on a method of a frozen dataclass
  mutates a single global; the patch must be undone
  per test (the existing tests use
  ``pytest.MonkeyPatch`` for this; the fake is scoped
  via the fixture).
- A fake subclass keeps the failure-injection next to
  the production adapter's class definition — the
  test reads as "this storage is configured to fail at
  ``bump_reason_counter``", not as "monkeypatch this
  method to raise".
- The fake exposes the same surface as
  ``RedisDLQStorage`` (Protocol subtype), so
  ``DeadLetterQueue`` and ``DeadLetterActions``
  consume it without ``MagicMock``.

Lives in ``tests/unit/events/_fakes.py`` (test-side
only; not exported from the production package).
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

from redis import exceptions as redis_exceptions

from kntgraph.core.result import Result
from kntgraph.infra.redis._dlq import RedisDLQStorage
from kntgraph.infra.redis._errors import MemoryError  # noqa: A004


class FailingRedisDLQStorage(RedisDLQStorage):
    """Subclasse real de ``RedisDLQStorage`` com falhas
    opt-in por flag. Cobre as branches defensivas dos
    ``translate_redis_call`` (``store.py:219, 252``) e
    os caminhos ``Ok(None)`` / ``Err`` em
    ``actions.py:128-130, 136-137, 178`` sem
    monkeypatching no cliente ``fakeredis``.

    Padrão "SUT no nível do adapter" — subclasse é um
    adapter real, com comportamento de produção
    intacto, exceto onde uma flag opt-in desvia o
    caminho. Cada flag é independente; testes
    configuram só as que a branch sob teste exercita.
    """

    # Class-level annotations so the type checker sees
    # the runtime-added attributes (set in __init__ via
    # object.__setattr__ because the parent is a frozen
    # dataclass).
    _fail_bump: ClassVar[bool]
    _fail_set_head: ClassVar[bool]
    _find_result_override: ClassVar[Result[Any, MemoryError] | None]
    _read_result_override: ClassVar[Result[Any, MemoryError] | None]
    _drop_result_override: ClassVar[Result[None, MemoryError] | None]
    _append_result_override: ClassVar[Result[str, MemoryError] | None]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # ``RedisDLQStorage`` is a frozen dataclass; bypass
        # the immutability check for the test-only state.
        object.__setattr__(self, "_fail_bump", False)
        object.__setattr__(self, "_fail_set_head", False)
        object.__setattr__(self, "_find_result_override", None)
        object.__setattr__(self, "_read_result_override", None)
        object.__setattr__(self, "_drop_result_override", None)
        object.__setattr__(self, "_append_result_override", None)
        # The ``DeadLetterQueue._set_agent_head`` path goes
        # through ``self._storage.client.hsetnx(...)`` (not
        # through any storage method). To make it fail
        # we swap the client for a wrapper that raises
        # ``RedisError`` when the opt-in flag is set. The
        # wrapper still forwards to the underlying client
        # when the flag is off.
        real_client = self.client
        fake_client = _HsetnxFailingClient(real_client, self)
        object.__setattr__(self, "client", fake_client)

    # -- opt-in API ----------------------------------------------------

    def fail_bump_reason_counter(self) -> None:
        """Make ``bump_reason_counter`` raise
        ``RedisError`` (the outer's
        ``translate_redis_call`` catches it and returns
        ``Err``). Exercises ``store.py:219``.
        """
        object.__setattr__(self, "_fail_bump", True)

    def fail_set_agent_head(self) -> None:
        """Make ``_set_agent_head`` raise
        ``RedisError``. Exercises ``store.py:252``.
        """
        object.__setattr__(self, "_fail_set_head", True)

    def make_find_return(self, result: Result[Any, MemoryError]) -> None:
        """Make ``find_by_event_id`` return the given
        result. Drives the ``Err`` branch in
        ``actions.py:128-130``.
        """
        object.__setattr__(self, "_find_result_override", result)

    def make_read_return(self, result: Result[Any, MemoryError]) -> None:
        """Make ``read`` return the given result. Drives
        the ``Ok(None)`` branch in ``actions.py:178``.
        """
        object.__setattr__(self, "_read_result_override", result)

    def make_drop_return(self, result: Result[None, MemoryError]) -> None:
        """Make ``drop_entry`` return the given result.
        Drives the ``Err`` branch in
        ``actions.py:136-137``.
        """
        object.__setattr__(self, "_drop_result_override", result)

    def make_append_return(self, result: Result[str, MemoryError]) -> None:
        """Make ``append`` return the given result.
        Drives the ``Ok(None)`` path in ``store.py:160``
        (``stream_id is None`` → agent-head step is
        skipped).
        """
        object.__setattr__(self, "_append_result_override", result)

    # -- overrides ------------------------------------------------------

    async def append(self, idem_key: str, payload: Any) -> Result[str, MemoryError]:
        override = self._append_result_override
        if override is not None:
            return cast("Result[str, MemoryError]", override)
        return await super().append(idem_key, payload)

    async def find_by_event_id(self, event_id: str) -> Result[str | None, MemoryError]:
        override = self._find_result_override
        if override is not None:
            return cast("Result[str | None, MemoryError]", override)
        return await super().find_by_event_id(event_id)

    async def read(self, stream_id: str) -> Result[Any, MemoryError]:
        override = self._read_result_override
        if override is not None:
            return cast("Result[Any, MemoryError]", override)
        return await super().read(stream_id)

    async def drop_entry(
        self, event_id: str, reason: str, stream_id: str
    ) -> Result[None, MemoryError]:
        override = self._drop_result_override
        if override is not None:
            return cast("Result[None, MemoryError]", override)
        return await super().drop_entry(event_id, reason, stream_id)

    async def bump_reason_counter(
        self, reason: str, delta: int
    ) -> Result[None, MemoryError]:
        if self._fail_bump:
            raise redis_exceptions.RedisError("simulated transport failure")
        return await super().bump_reason_counter(reason, delta)


class _HsetnxFailingClient:
    """Wrap a Redis client so that ``hsetnx`` raises
    ``RedisError`` when the parent ``FailingRedisDLQStorage``
    has its ``_fail_set_head`` flag set — but only on the
    second-and-later call. All other methods forward to
    the wrapped client.

    Needed because ``DeadLetterQueue._set_agent_head`` does
    ``self._storage.client.hsetnx(...)`` directly (not
    through a storage method), so a storage-level override
    is not enough — the failure must surface on the client.

    The first ``hsetnx`` call comes from
    ``RedisDLQStorage.append`` (placeholder claim on the
    event index — must succeed or the whole append fails
    before the agent-head step). The second call is the
    agent-head step on ``DLQ_AGENT_INDEX``. The counter
    scopes the failure to the second call so the
    storage's append path stays healthy.
    """

    def __init__(self, real_client: Any, storage: FailingRedisDLQStorage) -> None:
        self._real = real_client
        self._storage = storage
        self._hsetnx_call_count = 0

    async def hsetnx(self, *args: Any, **kwargs: Any) -> Any:
        self._hsetnx_call_count += 1
        if self._storage._fail_set_head and self._hsetnx_call_count >= 2:
            raise redis_exceptions.RedisError("simulated transport failure")
        return await self._real.hsetnx(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Forward every other attribute access to the
        wrapped client. ``hsetnx`` is overridden explicitly
        above; everything else (XADD, HSET, HGET, etc.)
        passes through transparently.
        """
        return getattr(self._real, name)


__all__ = ["FailingRedisDLQStorage"]
