# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
api._auth._helpers -- pure helpers for the API key
verifier pipeline.

These helpers are intentionally module-level (not
methods) so they stay CC = 1-2 and don't pollute the
:class:`RedisAPIKeyVerifier` body. They are stateless
and have no I/O.

This module is a private implementation detail of
``_auth``; the public surface is unchanged.
"""

from __future__ import annotations

import hashlib

from ...security import Principal, Role


def _digest(api_key: str) -> str:
    """SHA-256 hex digest of the API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _decode(raw: bytes) -> str:
    """Decode raw bytes (or str, for safety) into a UTF-8 string."""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8")
    return str(raw)


def _legacy_principal(agent_id: str) -> Principal:
    """
    Build a Principal from a legacy string binding.

    Heuristic for tenant_id (separator is ``.``, not ``/``,
    to remain compatible with the agent_id character class
    enforced by the EventLog trust boundary -- see B2):

      - ``tenant-A.agent-1`` -> ``tenant-A``
      - ``agent-1`` (no separator) -> ``agent-1``
        (treat the agent_id itself as the tenant,
        matching the legacy single-tenant convention)

    The role is fixed to ``agent``. There is no way to
    infer ``admin`` or ``service`` from a string, so
    deployments with those roles MUST run
    ``scripts/migrate_principals.py`` to upgrade the
    binding table before upgrading to 0.9.0.

    The ``key_id`` is set to ``legacy`` to flag audit
    logs (so operators can grep for unbound keys).
    """
    if not agent_id:
        raise ValueError("legacy agent_id is empty")
    return Principal.from_agent_id(agent_id, role=Role.agent, key_id="legacy")


__all__ = ["_decode", "_digest", "_legacy_principal"]
