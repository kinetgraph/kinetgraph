# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``infra/redis/_factory.py``.

Closes the infra/redis/_factory coverage gap (DEBT §3,
67% → 100%). The module exports 5 ``create_*`` factory
functions that build the framework's storage adapters
from ``Settings`` + an optional Redis client. The
factories are pure (no I/O — they instantiate the
adapters and return them) so the tests assert on the
adapter's class + the ``maxlen`` / ``ttl_seconds``
attribute that was resolved from the settings.

The uncovered branches were the "ttl from settings"
path in the three ShortMemoryStorage factories
(session / profile / continuity): when the caller
does not pass ``ttl_seconds=`` explicitly, the
factory reads the field from ``Settings`` and
forwards it to the adapter.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kntgraph.infra.config import Settings
from kntgraph.infra.redis._client import RedisLike
from kntgraph.infra.redis._dlq import RedisDLQStorage
from kntgraph.infra.redis._event_log import (
    MAXLEN_DEFAULT,
    RedisEventLogAdapter,
)
from kntgraph.infra.redis._factory import (
    create_continuity_storage,
    create_dlq_storage,
    create_event_log_storage,
    create_profile_storage,
    create_session_storage,
)
from kntgraph.infra.redis._memory import (
    RedisContinuityStorage,
    RedisProfileStorage,
    RedisSessionStorage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> RedisLike:
    return MagicMock(spec=RedisLike)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        stream_maxlen=500,
        global_stream_maxlen=2_000_000,
        session_ttl_seconds=3600,
        profile_ttl_seconds=None,
        continuity_ttl_seconds=86_400,
    )


# ---------------------------------------------------------------------------
# create_event_log_storage
# ---------------------------------------------------------------------------


class TestCreateEventLogStorage:
    def test_with_client_uses_default_maxlen(self, client: RedisLike) -> None:
        storage = create_event_log_storage(client=client)
        assert isinstance(storage, RedisEventLogAdapter)
        assert storage.maxlen == MAXLEN_DEFAULT

    def test_with_settings_resolves_stream_maxlen(
        self, client: RedisLike, settings: Settings
    ) -> None:
        storage = create_event_log_storage(settings=settings, client=client)
        assert storage.maxlen == 500

    def test_with_settings_no_client_uses_pool(self, settings: Settings) -> None:
        # No client → factory goes through
        # ``RedisPool.from_settings``. We assert on
        # the adapter's class and the resolved
        # ``maxlen``; the underlying pool's client is
        # not asserted (the pool is faked by ``Settings``
        # defaults in the test environment).
        storage = create_event_log_storage(settings=settings)
        assert isinstance(storage, RedisEventLogAdapter)
        assert storage.maxlen == 500

    def test_with_negative_maxlen_falls_back_to_default(
        self, client: RedisLike
    ) -> None:
        bad = Settings(stream_maxlen=-1)
        storage = create_event_log_storage(settings=bad, client=client)
        assert storage.maxlen == MAXLEN_DEFAULT

    def test_with_zero_maxlen_falls_back_to_default(self, client: RedisLike) -> None:
        bad = Settings(stream_maxlen=0)
        storage = create_event_log_storage(settings=bad, client=client)
        assert storage.maxlen == MAXLEN_DEFAULT

    def test_without_settings_or_client_uses_pool(self) -> None:
        # No settings, no client → pool + fresh
        # settings (the ``stream_maxlen`` fallback
        # path). Asserts on the adapter class and the
        # default ``maxlen``.
        storage = create_event_log_storage()
        assert isinstance(storage, RedisEventLogAdapter)
        assert storage.maxlen == MAXLEN_DEFAULT


# ---------------------------------------------------------------------------
# create_session_storage
# ---------------------------------------------------------------------------


class TestCreateSessionStorage:
    def test_with_client_uses_default_ttl(self, client: RedisLike) -> None:
        storage = create_session_storage(client=client)
        assert isinstance(storage, RedisSessionStorage)
        assert storage.ttl_seconds == 24 * 60 * 60  # Settings default

    def test_with_ttl_kwarg(self, client: RedisLike) -> None:
        storage = create_session_storage(client=client, ttl_seconds=120)
        assert storage.ttl_seconds == 120

    def test_with_settings_resolves_session_ttl(
        self, client: RedisLike, settings: Settings
    ) -> None:
        storage = create_session_storage(settings=settings, client=client)
        assert storage.ttl_seconds == 3600

    def test_with_settings_resolves_ttl_no_client(self, settings: Settings) -> None:
        storage = create_session_storage(settings=settings)
        assert isinstance(storage, RedisSessionStorage)
        assert storage.ttl_seconds == 3600


# ---------------------------------------------------------------------------
# create_profile_storage
# ---------------------------------------------------------------------------


class TestCreateProfileStorage:
    def test_with_client_uses_default_ttl(self, client: RedisLike) -> None:
        storage = create_profile_storage(client=client)
        assert isinstance(storage, RedisProfileStorage)
        assert storage.ttl_seconds is None  # Settings default

    def test_with_ttl_kwarg(self, client: RedisLike) -> None:
        storage = create_profile_storage(client=client, ttl_seconds=120)
        assert storage.ttl_seconds == 120

    def test_with_settings_resolves_profile_ttl(
        self, client: RedisLike, settings: Settings
    ) -> None:
        storage = create_profile_storage(settings=settings, client=client)
        assert storage.ttl_seconds is None  # explicit None

    def test_with_settings_resolves_ttl_no_client(self, settings: Settings) -> None:
        storage = create_profile_storage(settings=settings)
        assert isinstance(storage, RedisProfileStorage)
        assert storage.ttl_seconds is None


# ---------------------------------------------------------------------------
# create_continuity_storage
# ---------------------------------------------------------------------------


class TestCreateContinuityStorage:
    def test_with_client_uses_default_ttl(self, client: RedisLike) -> None:
        storage = create_continuity_storage(client=client)
        assert isinstance(storage, RedisContinuityStorage)
        assert storage.ttl_seconds == 90 * 24 * 60 * 60  # Settings default

    def test_with_ttl_kwarg(self, client: RedisLike) -> None:
        storage = create_continuity_storage(client=client, ttl_seconds=120)
        assert storage.ttl_seconds == 120

    def test_with_settings_resolves_continuity_ttl(
        self, client: RedisLike, settings: Settings
    ) -> None:
        storage = create_continuity_storage(settings=settings, client=client)
        assert storage.ttl_seconds == 86_400

    def test_with_settings_resolves_ttl_no_client(self, settings: Settings) -> None:
        storage = create_continuity_storage(settings=settings)
        assert isinstance(storage, RedisContinuityStorage)
        assert storage.ttl_seconds == 86_400


# ---------------------------------------------------------------------------
# create_dlq_storage
# ---------------------------------------------------------------------------


class TestCreateDlqStorage:
    def test_with_client_uses_default_maxlen(self, client: RedisLike) -> None:
        storage = create_dlq_storage(client=client)
        assert isinstance(storage, RedisDLQStorage)
        assert storage.maxlen == 1_000_000  # DLQ default

    def test_with_settings_resolves_global_maxlen(
        self, client: RedisLike, settings: Settings
    ) -> None:
        storage = create_dlq_storage(settings=settings, client=client)
        assert storage.maxlen == 2_000_000

    def test_with_negative_global_maxlen_falls_back(self, client: RedisLike) -> None:
        bad = Settings(global_stream_maxlen=-1)
        storage = create_dlq_storage(settings=bad, client=client)
        assert storage.maxlen == 1_000_000

    def test_with_zero_global_maxlen_falls_back(self, client: RedisLike) -> None:
        bad = Settings(global_stream_maxlen=0)
        storage = create_dlq_storage(settings=bad, client=client)
        assert storage.maxlen == 1_000_000
