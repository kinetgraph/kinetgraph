# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `claim_event_id_slot` — split into 3 phases.

Part of the RED phase for Iteration 1 (ADR-019). The
decomposition targets the god function in the original
`infra/idempotency.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


pytestmark = pytest.mark.asyncio


PLACEHOLDER = "PLACEHOLDER"
STREAM_ID = "1-0"
IDEM_KEY = "knt:eventids:abc"


def _make_pipeline_mock(stream_id: str = STREAM_ID) -> MagicMock:
    pipe = MagicMock()
    pipe.xadd = MagicMock(return_value=pipe)
    pipe.set = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[stream_id.encode()])
    return pipe


class TestCheckPhase:
    def test_module_importable(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            _check_phase,
            _claim_phase,
            _finalize_phase,
            claim_event_id_slot,
        )

        assert callable(_check_phase)
        assert callable(_claim_phase)
        assert callable(_finalize_phase)
        assert callable(claim_event_id_slot)

    async def test_returns_none_when_key_missing(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            _check_phase,
        )

        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        result = await _check_phase(redis, IDEM_KEY)
        assert result is None

    async def test_returns_existing_id_when_finalized(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            _check_phase,
        )

        redis = MagicMock()
        redis.get = AsyncMock(return_value=STREAM_ID.encode())
        result = await _check_phase(redis, IDEM_KEY)
        assert result == STREAM_ID

    async def test_raises_conflict_on_placeholder(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            IdempotencyConflict,
            _check_phase,
        )

        redis = MagicMock()
        redis.get = AsyncMock(return_value=PLACEHOLDER.encode())
        with pytest.raises(IdempotencyConflict):
            await _check_phase(redis, IDEM_KEY)


class TestClaimPhase:
    async def test_pipeline_executes_xadd_then_set_nx(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            _claim_phase,
        )

        pipe = _make_pipeline_mock()
        redis = MagicMock()
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)

        await _claim_phase(
            redis,
            stream_key="knt:agents:a-1:events",
            payload={"k": "v"},
            maxlen=1000,
            idem_key=IDEM_KEY,
        )
        redis.pipeline.assert_called_once_with(transaction=True)
        pipe.xadd.assert_called_once()
        pipe.set.assert_called_once_with(IDEM_KEY, PLACEHOLDER, nx=True)
        pipe.execute.assert_awaited_once()

    async def test_returns_stream_id_from_first_result(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            _claim_phase,
        )

        pipe = _make_pipeline_mock(stream_id="42-7")
        redis = MagicMock()
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)

        stream_id = await _claim_phase(redis, "stream", {}, 100, "idem")
        assert stream_id == "42-7"


class TestFinalizePhase:
    async def test_replaces_placeholder_with_stream_id(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            _finalize_phase,
        )

        redis = MagicMock()
        redis.set = AsyncMock()
        await _finalize_phase(redis, IDEM_KEY, STREAM_ID)
        redis.set.assert_awaited_once_with(IDEM_KEY, STREAM_ID)


class TestOrchestrator:
    async def test_returns_existing_id_on_replay(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            claim_event_id_slot,
        )

        redis = MagicMock()
        redis.get = AsyncMock(return_value="99-0".encode())
        result = await claim_event_id_slot(redis, IDEM_KEY, "stream", {}, 1000)
        assert result == "99-0"
        redis.pipeline.assert_not_called()

    async def test_full_path_claim_then_finalize(self):
        from kntgraph.infra.redis._event_log._idempotency import (
            claim_event_id_slot,
        )

        pipe = _make_pipeline_mock("123-0")
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=None)
        redis.set = AsyncMock()

        result = await claim_event_id_slot(redis, IDEM_KEY, "stream", {"k": "v"}, 100)
        assert result == "123-0"
        redis.set.assert_any_await(IDEM_KEY, "123-0")
