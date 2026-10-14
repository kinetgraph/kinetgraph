# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for ``claim_tool_queue_slot`` -- the tool-queue dedup helper
(ADR-083).

Part of the RED phase for ADR-083. The helper mirrors the
EventLog ``claim_event_id_slot`` shape
(``infra/redis/_event_log/_idempotency.py``) so the codebase has
one pattern for "Redis Streams write with dedup".

These tests are written first (TDD) and will FAIL until
``infra/redis/_tools/_idempotency.py`` ships.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


PLACEHOLDER: str = "PLACEHOLDER"
STREAM_ID: str = "1-0"
IDEM_KEY: str = "knt:tools:echo:idem:abc-123"
STREAM_KEY: str = "knt:tools:echo:queue"


def _make_pipeline_mock(stream_id: str = STREAM_ID) -> MagicMock:
    pipe = MagicMock()
    pipe.xadd = MagicMock(return_value=pipe)
    pipe.set = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[stream_id.encode()])
    return pipe


class TestModuleShape:
    async def test_module_importable(self):
        from kntgraph.infra.redis._tools._idempotency import (
            claim_tool_queue_slot,
            is_placeholder,
        )

        assert callable(claim_tool_queue_slot)
        assert callable(is_placeholder)


class TestCheckPhase:
    async def test_returns_none_when_key_missing(self):
        from kntgraph.infra.redis._tools._idempotency import _check_phase

        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        result = await _check_phase(redis, IDEM_KEY)
        assert result is None

    async def test_returns_existing_id_when_finalized(self):
        from kntgraph.infra.redis._tools._idempotency import _check_phase

        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"42-0")
        result = await _check_phase(redis, IDEM_KEY)
        assert result == "42-0"

    async def test_raises_conflict_on_placeholder(self):
        from kntgraph.infra.redis._errors import IdempotencyConflict
        from kntgraph.infra.redis._tools._idempotency import _check_phase

        redis = MagicMock()
        redis.get = AsyncMock(return_value=PLACEHOLDER.encode())
        with pytest.raises(IdempotencyConflict):
            await _check_phase(redis, IDEM_KEY)


class TestClaimPhase:
    async def test_pipeline_executes_xadd_then_set_nx(self):
        from kntgraph.infra.redis._tools._idempotency import _claim_phase

        pipe = _make_pipeline_mock("7-0")
        redis = MagicMock()
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)

        await _claim_phase(
            redis, STREAM_KEY, {"k": "v"}, maxlen=None, idem_key=IDEM_KEY
        )

        pipe.xadd.assert_called_once_with(STREAM_KEY, {"k": "v"}, maxlen=None)
        pipe.set.assert_called_once()
        set_kwargs = pipe.set.call_args.kwargs
        assert set_kwargs["nx"] is True
        assert set_kwargs["ex"] > 0

    async def test_returns_stream_id_from_first_result(self):
        from kntgraph.infra.redis._tools._idempotency import _claim_phase

        pipe = _make_pipeline_mock("1234-0")
        redis = MagicMock()
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)

        stream_id = await _claim_phase(
            redis, STREAM_KEY, {"payload": "x"}, maxlen=None, idem_key=IDEM_KEY
        )
        assert stream_id == "1234-0"


class TestFinalizePhase:
    async def test_replaces_placeholder_with_stream_id_and_sets_ttl(self):
        from kntgraph.infra.redis._tools._idempotency import _finalize_phase

        redis = MagicMock()
        redis.set = AsyncMock()
        await _finalize_phase(redis, IDEM_KEY, STREAM_ID, ttl_seconds=3600)
        redis.set.assert_awaited_once_with(IDEM_KEY, STREAM_ID, ex=3600)

    async def test_replaces_placeholder_without_ttl_if_zero(self):
        from kntgraph.infra.redis._tools._idempotency import _finalize_phase

        redis = MagicMock()
        redis.set = AsyncMock()
        await _finalize_phase(redis, IDEM_KEY, STREAM_ID, ttl_seconds=0)
        redis.set.assert_awaited_once_with(IDEM_KEY, STREAM_ID)


class TestOrchestrator:
    async def test_dedup_hit_short_circuits_no_pipeline(self):
        """When the index already holds a final stream_id, the
        orchestrator must NOT call XADD or pipeline; it returns
        ``Err(PersistenceError("tool_queue_dedup_hit"))`` so the
        ToolRouter caller can skip silently.
        """
        from kntgraph.core.result import PersistenceError
        from kntgraph.infra.redis._tools._idempotency import claim_tool_queue_slot

        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"99-0")
        redis.xadd = AsyncMock()
        redis.pipeline = MagicMock()
        result = await claim_tool_queue_slot(
            redis,
            idem_key=IDEM_KEY,
            stream_key=STREAM_KEY,
            payload={"payload": "{}"},
            maxlen=None,
            ttl_seconds=3600,
        )
        assert result.is_err()
        err = result.err_value()
        assert isinstance(err, PersistenceError)
        assert "tool_queue_dedup_hit" in str(err)
        redis.pipeline.assert_not_called()
        redis.xadd.assert_not_awaited()

    async def test_first_write_runs_full_path(self):
        """When the index is empty, the orchestrator runs the
        full path: claim (XADD + SET NX placeholder) then finalize
        (SET stream_id). Returns ``Ok(stream_id)``.
        """
        from kntgraph.infra.redis._tools._idempotency import claim_tool_queue_slot

        pipe = _make_pipeline_mock("77-0")
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)
        redis.set = AsyncMock()

        result = await claim_tool_queue_slot(
            redis,
            idem_key=IDEM_KEY,
            stream_key=STREAM_KEY,
            payload={"payload": "{}"},
            maxlen=None,
            ttl_seconds=3600,
        )
        assert result.is_ok()
        assert result.ok_value() == "77-0"
        redis.set.assert_any_await(IDEM_KEY, "77-0", ex=3600)

    async def test_concurrent_insert_returns_dedup_conflict_err(self):
        """When the index holds a PLACEHOLDER (a concurrent writer
        in flight), the orchestrator returns
        ``Err(PersistenceError("tool_queue_dedup_hit"))`` so the
        caller treats it as a skip (consistent with the
        finalised-id branch). The ToolRouter must distinguish
        this from the first-write path.
        """
        from kntgraph.core.result import PersistenceError
        from kntgraph.infra.redis._tools._idempotency import claim_tool_queue_slot

        redis = MagicMock()
        redis.get = AsyncMock(return_value=PLACEHOLDER.encode())
        result = await claim_tool_queue_slot(
            redis,
            idem_key=IDEM_KEY,
            stream_key=STREAM_KEY,
            payload={"payload": "{}"},
            maxlen=None,
            ttl_seconds=3600,
        )
        assert result.is_err()
        err = result.err_value()
        assert isinstance(err, PersistenceError)
        assert "tool_queue_dedup_hit" in str(err)


class TestIsPlaceholder:
    async def test_placeholder_string_is_true(self):
        from kntgraph.infra.redis._tools._idempotency import is_placeholder

        assert is_placeholder(PLACEHOLDER) is True

    async def test_stream_id_string_is_false(self):
        from kntgraph.infra.redis._tools._idempotency import is_placeholder

        assert is_placeholder("42-0") is False

    async def test_none_is_false(self):
        from kntgraph.infra.redis._tools._idempotency import is_placeholder

        assert is_placeholder(None) is False
