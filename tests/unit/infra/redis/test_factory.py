# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the Redis adapter factory — `create_event_log_storage`
and `create_dlq_storage`.

Part of the RED phase for Iteration 1 (ADR-019); extended
in 2026-07 to cover the ``Settings.stream_maxlen`` wiring
(see DEBT_TECHNICAL.md item 4).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestCreateEventLogStorage:
    def test_factory_module_importable(self):
        from kntgraph.infra.redis import create_event_log_storage

        assert callable(create_event_log_storage)

    def test_factory_returns_redis_adapter_by_default(self):
        from kntgraph.infra.redis import (
            RedisEventLogAdapter,
            create_event_log_storage,
        )

        fake_client = MagicMock()
        storage = create_event_log_storage(client=fake_client)
        assert isinstance(storage, RedisEventLogAdapter)

    def test_factory_uses_injected_settings(self):
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import (
            RedisEventLogAdapter,
            create_event_log_storage,
        )

        settings = Settings(redis_url="redis://x", redis_max_connections=10)
        fake_client = MagicMock()
        with patch("kntgraph.infra.redis._pool.RedisPool.from_settings") as mock_pool:
            mock_pool.return_value.client = fake_client
            storage = create_event_log_storage(settings=settings)
        assert isinstance(storage, RedisEventLogAdapter)

    def test_factory_uses_default_settings_when_none(self):
        from kntgraph.infra.redis import (
            RedisEventLogAdapter,
            create_event_log_storage,
        )

        fake_client = MagicMock()
        storage = create_event_log_storage(client=fake_client)
        assert isinstance(storage, RedisEventLogAdapter)

    def test_factory_reads_stream_maxlen_from_settings(self):
        """``Settings.stream_maxlen`` flows into the adapter
        when the caller supplies a ``Settings`` instance.

        DEBT_TECHNICAL.md item 4: ``Settings.stream_maxlen``
        was previously defined but never consumed. The
        factory must read it.
        """
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import (
            MAXLEN_DEFAULT,
            RedisEventLogAdapter,
            create_event_log_storage,
        )

        settings = Settings(
            redis_url="redis://x",
            redis_max_connections=10,
            stream_maxlen=42_000,
        )
        fake_client = MagicMock()
        storage = create_event_log_storage(settings=settings, client=fake_client)
        assert isinstance(storage, RedisEventLogAdapter)
        assert storage.maxlen == 42_000
        assert storage.maxlen != MAXLEN_DEFAULT  # confirms it was overridden

    def test_factory_falls_back_to_maxlen_default_when_setting_missing(
        self,
    ):
        """A non-positive ``stream_maxlen`` falls back to
        the module-level ``MAXLEN_DEFAULT`` so test contexts
        that construct a ``Settings`` with the field unset
        keep working.
        """
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import (
            MAXLEN_DEFAULT,
            RedisEventLogAdapter,
            create_event_log_storage,
        )

        settings = Settings(
            redis_url="redis://x",
            redis_max_connections=10,
            stream_maxlen=0,  # invalid -> fall back
        )
        fake_client = MagicMock()
        storage = create_event_log_storage(settings=settings, client=fake_client)
        assert isinstance(storage, RedisEventLogAdapter)
        assert storage.maxlen == MAXLEN_DEFAULT


class TestCreateDLQStorage:
    def test_dlq_factory_module_importable(self):
        from kntgraph.infra.redis import create_dlq_storage

        assert callable(create_dlq_storage)

    def test_dlq_factory_returns_redis_dlq_storage(self):
        from kntgraph.infra.redis import (
            RedisDLQStorage,
            create_dlq_storage,
        )

        fake_client = MagicMock()
        storage = create_dlq_storage(client=fake_client)
        assert isinstance(storage, RedisDLQStorage)

    def test_dlq_factory_reads_global_stream_maxlen_from_settings(self):
        """``Settings.global_stream_maxlen`` (not
        ``stream_maxlen``) flows into the DLQ adapter.

        The DLQ is a global stream (one per deployment, not
        per-tenant), so it uses the global cap.
        """
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis import create_dlq_storage

        settings = Settings(
            redis_url="redis://x",
            redis_max_connections=10,
            global_stream_maxlen=500_000,
        )
        fake_client = MagicMock()
        storage = create_dlq_storage(settings=settings, client=fake_client)
        assert storage.maxlen == 500_000

    def test_dlq_factory_falls_back_when_global_setting_missing(self):
        from kntgraph.infra.config import Settings
        from kntgraph.infra.redis._dlq import MAXLEN_DEFAULT

        from kntgraph.infra.redis import create_dlq_storage

        settings = Settings(
            redis_url="redis://x",
            redis_max_connections=10,
            global_stream_maxlen=0,  # invalid -> fall back
        )
        fake_client = MagicMock()
        storage = create_dlq_storage(settings=settings, client=fake_client)
        assert storage.maxlen == MAXLEN_DEFAULT
