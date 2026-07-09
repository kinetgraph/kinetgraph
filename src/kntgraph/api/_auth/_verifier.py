# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
api._auth._verifier -- ``APIKeyVerifier`` Protocol and
its default Redis-backed implementation.

The verifier hashes the ``X-API-Key`` header (sha256)
and delegates the lookup to an injected
``APIKeyStorage``. The wire format (JSON or legacy
string) is parsed here, into a ``Principal``.

Iteration 3 (ADR-019): the verifier no longer talks to
``redis.asyncio`` directly. It consumes the
``APIKeyStorage`` Protocol (see
``kntgraph.infra.redis._auth``) which owns the Redis
I/O. The verifier is a thin composition:

  1. Validate input (empty -> AuthError(missing)).
  2. Hash the API key (sha256).
  3. Delegate to ``storage.lookup(digest)``.
  4. Parse the bytes (JSON or legacy) -> Principal.

This module is a private implementation detail of
``_auth``; the public surface is unchanged.
"""

from __future__ import annotations

import json
from typing import Protocol

import structlog

from ...core.result import Err, Ok, Result
from ...infra.redis._auth import APIKeyStorage, RedisAPIKeyStorage
from ...security import Principal
from ._errors import AuthError
from ._helpers import _decode, _digest, _legacy_principal


logger = structlog.get_logger()


class APIKeyVerifier(Protocol):
    """
    Pluggable authentication. The default impl hashes
    the X-API-Key header and looks up the binding in
    Redis. Custom deployments inject their own.

    Returns a ``Principal`` (per ADR-017), not a bare
    ``agent_id``. The framework reads ``principal.role``
    and ``principal.tenant_id`` for authorisation at the
    request and tool boundaries.
    """

    async def verify(self, api_key: str) -> Result[Principal, AuthError]: ...


class RedisAPIKeyVerifier:
    """
    Default verifier. Hashes the X-API-Key header (sha256),
    delegates the Redis lookup to the injected
    ``APIKeyStorage``, then parses the wire format
    (JSON or legacy string) into a ``Principal``.

    Iteration 3 (ADR-019): the verifier is now a thin
    composition. The Redis I/O is owned by the storage.
    """

    HEADER_NAME = "x-api-key"

    def __init__(self, storage: APIKeyStorage) -> None:
        """
        Construct the verifier.

        Inject a ``APIKeyStorage`` (the Protocol). For the
        common case of constructing from a raw Redis-like
        client, use ``RedisAPIKeyVerifier.from_redis(client)``.

        The verifier is intentionally a thin composition:
        hash the key, delegate to the storage, parse the
        result into a ``Principal``.
        """
        self._storage = storage

    @classmethod
    def from_redis(cls, client) -> "RedisAPIKeyVerifier":
        """
        Convenience constructor for the common case:
        build the verifier from a raw Redis-like client.

        Kept for back-compat with call sites that hold a
        raw client (most tests, fmh_app's
        ``StaticAPIKeyVerifier`` shim). New code should
        construct the ``APIKeyStorage`` directly and
        inject it.
        """
        storage = RedisAPIKeyStorage(client=client)
        return cls(storage=storage)

    async def verify(self, api_key: str) -> Result[Principal, AuthError]:
        """
        Verify the API key and return the bound
        ``Principal``. Errors:

          - ``AuthError(kind='missing')`` if the key
            is empty.
          - ``AuthError(kind='forbidden')`` if the
            key is not bound to any principal.
          - ``AuthError(kind='malformed')`` if the
            binding is corrupt (neither valid JSON
            nor a legacy string).

        The legacy string path is preserved for the
        0.9.x migration window and removed in 0.10.0
        (ADR-017 §7).
        """
        if not api_key:
            return Err(
                AuthError(
                    "missing",
                    "X-API-Key header is required",
                )
            )

        digest = _digest(api_key)

        # Delegate to the storage. A storage error becomes
        # a forbidden (we don't reveal whether the key
        # exists or whether the storage is down).
        lookup = await self._storage.lookup(digest)
        if lookup.is_err():
            logger.warning(
                "api_key.verify.storage_error",
                digest=digest,
                error=str(lookup.err_value()),
            )
            return Err(
                AuthError(
                    "forbidden",
                    "API key is not recognised",
                )
            )
        raw = lookup.ok_value()
        if raw is None:
            return Err(
                AuthError(
                    "forbidden",
                    "API key is not recognised",
                )
            )

        decoded = _decode(raw)

        # Try JSON first (Zero-Trust).
        stripped = decoded.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as e:
                return Err(
                    AuthError(
                        "malformed",
                        f"API key binding is not valid JSON: {e}",
                    )
                )
            try:
                return Ok(Principal.from_json(payload))
            except (ValueError, KeyError) as e:
                return Err(
                    AuthError(
                        "malformed",
                        f"API key binding JSON missing required fields: {e}",
                    )
                )

        # Legacy fallback: plain agent_id string.
        return Ok(_legacy_principal(decoded))


__all__ = ["APIKeyVerifier", "RedisAPIKeyVerifier"]
