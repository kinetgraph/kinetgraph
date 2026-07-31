# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/redis/_memory/_solution.py``
(``RedisSolutionStore``).

Filename note: the test file lives at
``test_solution_store.py`` (not ``test_solution.py``)
because ``tests/unit/knowledge/graph/_sub/test_solution.py``
already exists -- pytest's default discovery raises
``_pytest.collect.CollectError`` when two test files share
a basename that collides on the import path. The
uniqueness suffix is ``_store``; the production module
is unaffected.

The Solution cache is the read-side of the Solution tier
(ADR-010 / ADR-049). Its protocol is unique among the
short-memory adapters:

  - ``find_match`` returns ``Optional[CachedSolution]``
    directly (not a ``Result``). The lookup system treats
    a Redis-side failure as a miss so the LLM fallback
    can take over; the contract is "fail-open on the
    read side, fail-closed on the write side".
  - ``put`` returns ``Result[None, SolutionStoreError]``
    and uses a transactional pipeline so the TTL is
    applied atomically with the value.
  - The wire format is one Redis Hash per tool
    (``knt:solution:<tool_name>``); the field is the
    ``params_fingerprint``.

The tests below exercise:
  - The ``find_match`` happy path (hit / miss / below
    threshold).
  - The ``find_match`` failure modes (Redis exception,
    corrupt payload) -- both degrade to a miss.
  - The ``put`` success path (writes to the per-tool
    Hash; applies TTL when configured).
  - The ``put`` failure modes (Redis exception on the
    pipeline, payload that JSON cannot encode).
  - The ``delete`` idempotent behaviour.
  - The ``iter_keys`` / ``read_all`` operator-side
    helpers.
  - The ``SolutionStoreLike`` Protocol check
    (constructor rejects non-conforming stores).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.agents.memory.solution_lookup import (
    CachedSolution,
    InMemorySolutionStore,
    SolutionLookupSystem,
    SolutionStoreLike,
)
from kntgraph.infra.redis._memory._solution import (
    SOLUTION_KEY_PREFIX,
    RedisSolutionStore,
    SolutionStoreDecodeError,
    SolutionStoreError,
    SolutionStoreSerializationError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Per AGENTS.md §7.3: ``asyncio_mode = "strict"`` requires
# an explicit ``@pytest.mark.asyncio`` on every
# ``async def test_*``; the project does NOT use the
# global ``pytestmark`` pattern.


@pytest.fixture
def client() -> MagicMock:
    """A bare ``RedisLike`` mock.

    The default returns ``None`` for every read so each
    test must wire the specific call it cares about.
    """
    client = MagicMock()
    client.hget = AsyncMock(return_value=None)
    client.hgetall = AsyncMock(return_value={})
    client.hdel = AsyncMock(return_value=1)
    client.hscan_iter = MagicMock(return_value=iter([]))
    # Pipeline mock: each chained call returns the pipe
    # itself so ``pipe.hset(...).expire(...).execute()``
    # works. ``execute`` is async.
    pipe = MagicMock()
    pipe.hset = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.hdel = MagicMock(return_value=pipe)
    pipe.delete = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[1, 1])
    client.pipeline = MagicMock(return_value=pipe)
    return client


@pytest.fixture
def store(client: MagicMock) -> RedisSolutionStore:
    return RedisSolutionStore(client=client)


@pytest.fixture
def store_with_ttl(client: MagicMock) -> RedisSolutionStore:
    return RedisSolutionStore(client=client, ttl_seconds=3600)


def _sample_solution(
    *,
    tool_name: str = "knowledge_lookup",
    params_fingerprint: str = "fp-export-v1",
    confidence: int = 5,
    result: dict | None = None,
    source_completion_event_id: str = "11111111-1111-1111-1111-111111111111",
) -> CachedSolution:
    return CachedSolution(
        tool_name=tool_name,
        params_fingerprint=params_fingerprint,
        confidence=confidence,
        result=result if result is not None else {"answer": "click settings"},
        source_completion_event_id=source_completion_event_id,
    )


def _encode(solution: CachedSolution) -> bytes:
    return json.dumps(
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
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


class TestKeyLayout:
    def test_key_uses_prefix_and_tool_name(self) -> None:
        assert RedisSolutionStore._key("knowledge_lookup") == (
            SOLUTION_KEY_PREFIX + "knowledge_lookup"
        )

    def test_key_prefix_is_namespaced(self) -> None:
        # Sanity: the prefix is namespaced so a
        # future ``SCAN`` over ``knt:solution:*`` is
        # bounded to the Solution tier.
        assert SOLUTION_KEY_PREFIX == "knt:solution:"


# ---------------------------------------------------------------------------
# find_match
# ---------------------------------------------------------------------------


class TestFindMatch:
    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self, store: RedisSolutionStore) -> None:
        # Default ``hget`` returns ``None`` → miss.
        out = await store.find_match(
            tool_name="knowledge_lookup",
            params_fingerprint="fp-missing",
            min_confidence=1,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_returns_solution_on_hit(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        solution = _sample_solution()
        client.hget = AsyncMock(return_value=_encode(solution))
        out = await store.find_match(
            tool_name=solution.tool_name,
            params_fingerprint=solution.params_fingerprint,
            min_confidence=3,
        )
        assert out is not None
        assert out.tool_name == solution.tool_name
        assert out.params_fingerprint == solution.params_fingerprint
        assert out.confidence == solution.confidence
        assert out.result == solution.result

    @pytest.mark.asyncio
    async def test_returns_none_below_min_confidence(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        solution = _sample_solution(confidence=2)
        client.hget = AsyncMock(return_value=_encode(solution))
        out = await store.find_match(
            tool_name=solution.tool_name,
            params_fingerprint=solution.params_fingerprint,
            min_confidence=3,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_returns_none_on_redis_error(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        # A failing lookup degrades to a miss so the LLM
        # fallback can take over (ADR-049 §2.1.3).
        client.hget = AsyncMock(side_effect=ConnectionError("redis down"))
        out = await store.find_match(
            tool_name="knowledge_lookup",
            params_fingerprint="fp-x",
            min_confidence=1,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_returns_none_on_corrupt_json(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        client.hget = AsyncMock(return_value=b"not-json-{")
        out = await store.find_match(
            tool_name="knowledge_lookup",
            params_fingerprint="fp-x",
            min_confidence=1,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_returns_none_on_non_mapping_payload(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        client.hget = AsyncMock(return_value=b'"just a string"')
        out = await store.find_match(
            tool_name="knowledge_lookup",
            params_fingerprint="fp-x",
            min_confidence=1,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_returns_none_on_missing_required_fields(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        # ``confidence`` is missing — the decode step
        # coerces to ``0``, but then ``confidence <
        # min_confidence`` so it still degrades to a
        # miss (the lookup-system's "low confidence"
        # arm).
        client.hget = AsyncMock(
            return_value=b'{"tool_name": "x", "params_fingerprint": "fp"}'
        )
        out = await store.find_match(
            tool_name="knowledge_lookup",
            params_fingerprint="fp",
            min_confidence=1,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_keys_use_correct_namespace(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        solution = _sample_solution()
        client.hget = AsyncMock(return_value=_encode(solution))
        await store.find_match(
            tool_name=solution.tool_name,
            params_fingerprint=solution.params_fingerprint,
            min_confidence=1,
        )
        client.hget.assert_awaited_once_with(
            SOLUTION_KEY_PREFIX + solution.tool_name,
            solution.params_fingerprint,
        )


# ---------------------------------------------------------------------------
# put
# ---------------------------------------------------------------------------


class TestPut:
    @pytest.mark.asyncio
    async def test_puts_without_ttl_when_unset(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        solution = _sample_solution()
        result = await store.put(solution)
        assert result.is_ok()
        # The pipeline must NOT include an ``expire``
        # call when ``ttl_seconds`` is ``None``.
        pipe = client.pipeline.return_value
        pipe.expire.assert_not_called()
        pipe.hset.assert_called_once()
        pipe.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_puts_with_ttl_when_set(
        self, store_with_ttl: RedisSolutionStore, client: MagicMock
    ) -> None:
        solution = _sample_solution()
        result = await store_with_ttl.put(solution)
        assert result.is_ok()
        pipe = client.pipeline.return_value
        pipe.hset.assert_called_once()
        pipe.expire.assert_called_once_with(
            SOLUTION_KEY_PREFIX + solution.tool_name, 3600
        )
        pipe.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_err_on_redis_failure(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        client.pipeline.return_value.execute = AsyncMock(
            side_effect=ConnectionError("redis down")
        )
        result = await store.put(_sample_solution())
        assert result.is_err()
        assert isinstance(result.err_value(), SolutionStoreError)
        assert not isinstance(result.err_value(), SolutionStoreSerializationError)

    @pytest.mark.asyncio
    async def test_returns_serialization_err_on_unencodable(
        self, store: RedisSolutionStore
    ) -> None:
        # ``set`` is not JSON-encodable by default; the
        # adapter's ``default=str`` falls back, so we
        # need a value that breaks ``str()`` too. A
        # bare object whose ``__str__`` raises.
        class Boom:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        sol = CachedSolution(
            tool_name="knowledge_lookup",
            params_fingerprint="fp",
            confidence=5,
            result={"k": Boom()},
            source_completion_event_id="x",
        )
        result = await store.put(sol)
        assert result.is_err()
        assert isinstance(result.err_value(), SolutionStoreSerializationError)

    @pytest.mark.asyncio
    async def test_keys_use_correct_namespace(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        solution = _sample_solution()
        await store.put(solution)
        pipe = client.pipeline.return_value
        pipe.hset.assert_called_once()
        key, field, payload = pipe.hset.call_args.args
        assert key == SOLUTION_KEY_PREFIX + solution.tool_name
        assert field == solution.params_fingerprint
        # Payload is the JSON-encoded solution.
        decoded = json.loads(payload)
        assert decoded["tool_name"] == solution.tool_name
        assert decoded["confidence"] == solution.confidence


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_is_idempotent(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        client.hdel = AsyncMock(return_value=0)
        result = await store.delete("knowledge_lookup", "fp")
        assert result.is_ok()

    @pytest.mark.asyncio
    async def test_returns_err_on_redis_failure(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        client.hdel = AsyncMock(side_effect=ConnectionError("redis down"))
        result = await store.delete("knowledge_lookup", "fp")
        assert result.is_err()
        assert isinstance(result.err_value(), SolutionStoreError)

    @pytest.mark.asyncio
    async def test_keys_use_correct_namespace(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        await store.delete("knowledge_lookup", "fp-1")
        client.hdel.assert_awaited_once_with(
            SOLUTION_KEY_PREFIX + "knowledge_lookup", "fp-1"
        )


# ---------------------------------------------------------------------------
# iter_keys + read_all (operator-side helpers)
# ---------------------------------------------------------------------------


class TestIterKeys:
    @pytest.mark.asyncio
    async def test_iter_decodes_bytes_fields(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        # ``hscan_iter`` (per ``redis.asyncio.Redis``)
        # yields ``(field, value)`` tuples. The adapter
        # discards the value (the fingerprint is the
        # field) and yields the decoded field.
        calls: list[tuple] = []

        async def fake_iter(*args, **kwargs):
            calls.append((args, kwargs))
            yield (b"fp-1", b"value-1")
            yield ("fp-2", "value-2")
            yield (b"fp-3", b"value-3")

        client.hscan_iter = fake_iter
        out = [k async for k in store.iter_keys("knowledge_lookup")]
        assert out == ["fp-1", "fp-2", "fp-3"]
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == (SOLUTION_KEY_PREFIX + "knowledge_lookup",)
        assert kwargs == {"match": None, "count": 100}

    @pytest.mark.asyncio
    async def test_iter_tolerates_bare_field_iterables(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        # Defensive: some test doubles return bare
        # fields (no ``(field, value)`` wrapping). The
        # adapter treats both shapes uniformly.
        async def fake_iter(*_args, **_kwargs):
            yield b"fp-1"
            yield "fp-2"

        client.hscan_iter = fake_iter
        out = [k async for k in store.iter_keys("knowledge_lookup")]
        assert out == ["fp-1", "fp-2"]

    @pytest.mark.asyncio
    async def test_iter_skips_empty_fields(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        async def fake_iter(*_args, **_kwargs):
            yield (b"fp-1", b"v")
            yield (None, b"v")  # ``decode_value`` returns ``None`` → skipped
            yield (b"fp-2", b"v")

        client.hscan_iter = fake_iter
        out = [k async for k in store.iter_keys("knowledge_lookup")]
        assert out == ["fp-1", "fp-2"]


class TestReadAll:
    @pytest.mark.asyncio
    async def test_read_all_decodes_each_entry(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        solution = _sample_solution()
        client.hgetall = AsyncMock(
            return_value={solution.params_fingerprint: _encode(solution)}
        )
        out = await store.read_all(solution.tool_name)
        assert solution.params_fingerprint in out
        assert out[solution.params_fingerprint].tool_name == solution.tool_name

    @pytest.mark.asyncio
    async def test_read_all_skips_corrupt_entries(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        solution = _sample_solution()
        client.hgetall = AsyncMock(
            return_value={
                solution.params_fingerprint: _encode(solution),
                "corrupt": b"not-json-{",
            }
        )
        out = await store.read_all(solution.tool_name)
        assert solution.params_fingerprint in out
        assert "corrupt" not in out

    @pytest.mark.asyncio
    async def test_read_all_returns_empty_on_redis_error(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        client.hgetall = AsyncMock(side_effect=ConnectionError("redis down"))
        out = await store.read_all("knowledge_lookup")
        assert out == {}


# ---------------------------------------------------------------------------
# SolutionStoreLike protocol compatibility
# ---------------------------------------------------------------------------


class TestProtocolCompatibility:
    @pytest.mark.asyncio
    async def test_satisfies_protocol(self, store: RedisSolutionStore) -> None:
        # ``SolutionStoreLike`` is ``@runtime_checkable``.
        assert isinstance(store, SolutionStoreLike)

    @pytest.mark.asyncio
    async def test_solution_lookup_system_accepts_redis_store(
        self, store: RedisSolutionStore, client: MagicMock
    ) -> None:
        # The canonical happy path: register a Solution,
        # look it up through the system, confirm the
        # ``tool.<name>.completed`` event is synthesised.
        solution = _sample_solution()
        client.hget = AsyncMock(return_value=_encode(solution))

        from datetime import datetime, timezone

        from kntgraph.core.world import AgentView, World
        from kntgraph.core.world.components import ToolCallRequest

        system = SolutionLookupSystem(
            solution_store=store,
            allowlist=frozenset({"knowledge_lookup"}),
            min_confidence=3,
        )

        # Build a minimal World with the ToolCallRequest
        # on the ``tool_requests`` slot, the way the
        # tool-call overlay would have laid it out.
        req = ToolCallRequest(
            request_event_id="22222222-2222-2222-2222-222222222222",
            tool_name="knowledge_lookup",
            agent_id="agent-1",
            params={"question_id": "export-data-v1"},
            requested_at=datetime.now(timezone.utc),
        )
        view = AgentView(
            agent_id="agent-1",
            components={"tool_requests": {req.request_event_id: req}},
        )
        world = World(tick=1, storage=None, views={"agent-1": view})

        # First ``__call__`` queues the request;
        # ``run_pending_lookups`` drains it.
        system(world)
        await system.run_pending_lookups()
        # Second ``__call__`` returns the synthetic
        # completion from the previous tick's queue.
        completions = system(world)
        assert len(completions) == 1
        assert completions[0].event_type == "tool.knowledge_lookup.completed"
        assert completions[0].data["source"] == "solution_lookup"

    def test_lookup_system_rejects_non_protocol_store(self) -> None:
        # The defensive ``isinstance`` check in the
        # ``SolutionLookupSystem.__init__`` rejects
        # stores that don't implement ``find_match``.

        class NotAStore:
            pass

        with pytest.raises(TypeError):
            SolutionLookupSystem(solution_store=NotAStore())

    @pytest.mark.asyncio
    async def test_in_memory_store_also_satisfies_protocol(self) -> None:
        # Sanity: the InMemorySolutionStore still
        # satisfies the Protocol after the new Redis
        # adapter joins the family.
        in_memory = InMemorySolutionStore()
        assert isinstance(in_memory, SolutionStoreLike)


# ---------------------------------------------------------------------------
# Decode error class (exposed for defensive call sites)
# ---------------------------------------------------------------------------


class TestErrors:
    def test_decode_error_carries_context(self) -> None:
        err = SolutionStoreDecodeError(
            "bad json",
            tool_name="knowledge_lookup",
            params_fingerprint="fp-x",
        )
        assert err.tool_name == "knowledge_lookup"
        assert err.params_fingerprint == "fp-x"
        assert "bad json" in str(err)

    def test_serialization_error_carries_context(self) -> None:
        err = SolutionStoreSerializationError(
            "cannot encode",
            tool_name="knowledge_lookup",
            params_fingerprint="fp-x",
        )
        assert err.tool_name == "knowledge_lookup"

    def test_base_error_is_typed(self) -> None:
        err = SolutionStoreError("redis boom", tool_name="x", params_fingerprint="y")
        assert err.tool_name == "x"
        assert err.params_fingerprint == "y"
