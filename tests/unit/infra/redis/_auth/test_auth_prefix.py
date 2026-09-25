# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the auth-layer ADR-076 key prefix
plumbing (DEBT §2.35 follow-up #2).

Closes the auth-adapter gap left by the §2.35 PR.
``RedisAPIKeyStorage`` accepts ``key_prefix=`` and
composes every ``knt:api:keys:<digest>`` key with
the operator's ``KNT_REDIS_KEY_PREFIX``. The module-
level :func:`storage_key` helper is the canonical
composition point (used by the migration script and
external code); the storage instance has the same
logic via :meth:`RedisAPIKeyStorage.storage_key`.

Behaviour contract:

  - ``key_prefix=""`` (default): byte-for-byte identical
    to pre-076 wire format.
  - ``key_prefix="acme-billing:"``: every read/write is
    at ``acme-billing:knt:api:keys:<digest>`` so two
    services sharing one Redis do not cross-talk via
    the binding table.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kntgraph.infra.redis._auth import RedisAPIKeyStorage, storage_key


def _fake_redis() -> MagicMock:
    """Minimal ``RedisLike`` double for the auth storage.

    Each call records the key argument so the tests
    can assert on the namespaced wire format.
    """
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    return redis


# ---------------------------------------------------------------------------
# module-level ``storage_key`` helper
# ---------------------------------------------------------------------------


class TestStorageKeyHelper:
    """The module-level :func:`storage_key` is the
    canonical composition point -- used by the migration
    script and any external code that needs the wire
    format without constructing a storage instance.
    """

    def test_empty_prefix_returns_unprefixed_key(self) -> None:
        """Empty prefix is the byte-for-byte identical
        pre-076 wire format. The legacy call sites (e.g.
        ``scripts/migrate_principals.py``) keep working.
        """
        assert storage_key("", "abc123") == "knt:api:keys:abc123"

    def test_prefix_with_trailing_colon(self) -> None:
        """The canonical operator-facing shape:
        ``"acme-billing:"`` produces
        ``"acme-billing:knt:api:keys:abc123"``.
        """
        assert storage_key("acme-billing:", "abc123") == (
            "acme-billing:knt:api:keys:abc123"
        )

    def test_prefix_without_trailing_colon(self) -> None:
        """A prefix without a trailing colon produces a
        concatenated key without an inserted separator
        (matches the :func:`namespaced` contract).
        """
        assert storage_key("acme", "abc123") == "acmeknt:api:keys:abc123"

    def test_compound_prefix(self) -> None:
        """Compound prefixes (``"p1.p2:"``) compose in
        front of the suffix unchanged.
        """
        assert storage_key("p1.p2:", "abc123") == "p1.p2:knt:api:keys:abc123"


# ---------------------------------------------------------------------------
# ``RedisAPIKeyStorage`` -- constructor + ``storage_key`` method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRedisAPIKeyStoragePrefix:
    """The storage's own ``storage_key(digest)`` method
    applies the configured ``key_prefix`` to every key
    the storage reads or writes.
    """

    async def test_default_prefix_writes_unprefixed_key(self) -> None:
        """Empty ``key_prefix`` (the default) keeps the
        pre-ADR-076 wire format -- ``get`` is awaited
        with ``"knt:api:keys:abc123"``.
        """
        redis = _fake_redis()
        storage = RedisAPIKeyStorage(client=redis)

        await storage.lookup("abc123")

        redis.get.assert_awaited_once_with("knt:api:keys:abc123")

    async def test_prefixed_lookup_uses_namespaced_key(self) -> None:
        """Non-empty ``key_prefix`` namespaces the
        ``get`` -- the canonical multi-service-on-one-Redis
        use case from ADR-076 §1.1.
        """
        redis = _fake_redis()
        storage = RedisAPIKeyStorage(client=redis, key_prefix="acme-billing:")

        await storage.lookup("abc123")

        redis.get.assert_awaited_once_with("acme-billing:knt:api:keys:abc123")

    async def test_prefixed_store_uses_namespaced_key(self) -> None:
        """``store`` composes the prefix the same way
        ``lookup`` does. Without this symmetry a prefixed
        write would land at the unprefixed key and the
        prefixed read would miss it.
        """
        redis = _fake_redis()
        storage = RedisAPIKeyStorage(client=redis, key_prefix="acme-billing:")

        await storage.store("abc123", b"payload")

        redis.set.assert_awaited_once_with(
            "acme-billing:knt:api:keys:abc123", b"payload"
        )

    async def test_prefixed_delete_uses_namespaced_key(self) -> None:
        """``delete`` composes the prefix the same way.
        Without this symmetry an operator's delete on
        the prefixed key would silently miss (and the
        binding would remain live).
        """
        redis = _fake_redis()
        storage = RedisAPIKeyStorage(client=redis, key_prefix="acme-billing:")

        await storage.delete("abc123")

        redis.delete.assert_awaited_once_with("acme-billing:knt:api:keys:abc123")

    async def test_two_prefixes_do_not_cross_talk(self) -> None:
        """Two storage instances with different prefixes
        on the same Redis MUST NOT see each other's
        bindings -- the canonical multi-service
        guarantee from ADR-076 §1.1.
        """
        redis = _fake_redis()

        acme_storage = RedisAPIKeyStorage(client=redis, key_prefix="acme:")
        crm_storage = RedisAPIKeyStorage(client=redis, key_prefix="crm:")

        await acme_storage.store("abc", b"acme-payload")
        await crm_storage.store("abc", b"crm-payload")

        # Each prefix wrote to its own key.
        assert redis.set.await_args_list == [
            (("acme:knt:api:keys:abc", b"acme-payload"), {}),
            (("crm:knt:api:keys:abc", b"crm-payload"), {}),
        ]

    async def test_invalid_prefix_rejected_at_construction(self) -> None:
        """``validate_prefix`` runs once at construction;
        a malformed prefix raises ``ValueError``
        immediately instead of producing malformed
        Redis keys at lookup time.
        """
        with pytest.raises(ValueError, match="redis_key_prefix"):
            RedisAPIKeyStorage(client=_fake_redis(), key_prefix="acme:{")

    async def test_trailing_knt_collision_rejected(self) -> None:
        """The validator rejects ``acme-billing:knt:``
        (the framework's reserved namespace collision,
        fixed in :mod:`kntgraph.infra.redis._prefix`).
        Without this guard the composed key would
        duplicate the ``knt:`` literal.
        """
        with pytest.raises(ValueError, match="redis_key_prefix"):
            RedisAPIKeyStorage(client=_fake_redis(), key_prefix="acme-billing:knt:")


# ---------------------------------------------------------------------------
# ``create_api_key_storage`` factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCreateAPIKeyStorageFactory:
    """The factory mirrors the pattern of the other
    adapters: explicit ``key_prefix=`` wins; otherwise
    ``Settings.redis_key_prefix`` is used; otherwise
    empty string (the pre-076 default).
    """

    async def test_explicit_key_prefix_wins(self) -> None:
        """The factory honours an explicit override."""
        from kntgraph.infra.redis import create_api_key_storage

        storage = create_api_key_storage(client=_fake_redis(), key_prefix="acme:")
        assert storage.key_prefix == "acme:"

    async def test_empty_settings_falls_back_to_empty_string(self) -> None:
        """No settings, no override: ``key_prefix=""``
        (the pre-076 default).
        """
        from kntgraph.infra.redis import create_api_key_storage

        storage = create_api_key_storage(client=_fake_redis())
        assert storage.key_prefix == ""

    async def test_settings_redis_key_prefix_used(self) -> None:
        """When the operator sets ``KNT_REDIS_KEY_PREFIX``,
        the factory threads it through.
        """
        from kntgraph.infra.redis import create_api_key_storage
        from kntgraph.infra.config import fresh_settings

        settings = fresh_settings()
        settings.redis_key_prefix = "acme-billing:"
        storage = create_api_key_storage(settings=settings, client=_fake_redis())
        assert storage.key_prefix == "acme-billing:"


# ---------------------------------------------------------------------------
# ``RedisAPIKeyVerifier.from_redis``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVerifierFromRedisPrefix:
    """The verifier convenience constructor threads the
    prefix to the underlying storage.
    """

    async def test_from_redis_propagates_key_prefix_to_storage(self) -> None:
        from kntgraph.api.auth import RedisAPIKeyVerifier

        verifier = RedisAPIKeyVerifier.from_redis(
            _fake_redis(), key_prefix="acme-billing:"
        )
        assert verifier._storage.key_prefix == "acme-billing:"

    async def test_from_redis_default_key_prefix_is_empty(self) -> None:
        """The default ``key_prefix=""`` preserves the
        pre-076 wire format -- back-compat for the
        existing tests that construct the verifier
        without a prefix kwarg.
        """
        from kntgraph.api.auth import RedisAPIKeyVerifier

        verifier = RedisAPIKeyVerifier.from_redis(_fake_redis())
        assert verifier._storage.key_prefix == ""

    async def test_from_redis_invalid_prefix_rejected(self) -> None:
        """The verifier's prefix is validated at the
        storage's ``__post_init__`` -- a malformed
        prefix raises immediately.
        """
        from kntgraph.api.auth import RedisAPIKeyVerifier

        with pytest.raises(ValueError, match="redis_key_prefix"):
            RedisAPIKeyVerifier.from_redis(_fake_redis(), key_prefix="acme:knt:")
