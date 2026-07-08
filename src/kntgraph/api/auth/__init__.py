# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
api.auth -- API key authentication for the HTTP gateway
(ADR-012 + ADR-017 Level 2).

The gateway authenticates a request via the ``X-API-Key``
header and returns a ``Principal`` (per ADR-017 §2.1).

Binding table format
--------------------

The Redis binding ``fmh:api:keys:<sha256>`` stores a
JSON payload:

    {
        "agent_id":  "tenant-A.agent-1",
        "role":      "agent",        # admin | agent | service
        "tenant_id": "tenant-A",    # null for admin
        "key_id":    "k-2026-06-23-001"
    }

Legacy compatibility (deprecated): if the stored value
is **not JSON** (i.e. a plain string), the verifier
treats it as ``agent_id`` and constructs:

    Principal(
        agent_id=raw,
        role=Role.agent,
        tenant_id=raw.partition("/")[0],   # heuristic
        key_id="legacy",
    )

A `scripts/migrate_principals.py` migrates the table
to the JSON format. Legacy mode is removed in 0.10.0.

Implementation layout
---------------------

The 506-L monolithic ``auth.py`` was split into a
``_auth/`` sub-package so each file is under the 500-L
guideline (AGENTS.md §3.1):

  - ``_auth._errors`` -- ``AuthError``.
  - ``_auth._verifier`` -- ``APIKeyVerifier`` Protocol
    and ``RedisAPIKeyVerifier`` (the default
    implementation).
  - ``_auth._helpers`` -- the three pure helpers
    (``_digest``, ``_decode``, ``_legacy_principal``)
    used by the verifier pipeline.
  - ``_auth._dependencies`` -- the FastAPI ``Depends``
    helpers (``check_agent_binding`` and
    ``bind_principal_dependency``).

The unused ``require_principal`` / ``require_role`` /
``require_tenant`` helpers (workflow P1 #3; tracked
in ``DEBT_TECHNICAL.md`` A.4) were removed in this
split -- they had no call sites in the framework or
verticals. The two retained helpers cover every
authenticated endpoint in the codebase.

External imports of the form
``from kntgraph.api.auth import X`` continue to
work via the re-exports below.
"""

from __future__ import annotations

from .._auth._dependencies import bind_principal_dependency, check_agent_binding
from .._auth._errors import AuthError
from .._auth._helpers import _legacy_principal
from .._auth._verifier import APIKeyVerifier, RedisAPIKeyVerifier


__all__ = [
    "APIKeyVerifier",
    "AuthError",
    "RedisAPIKeyVerifier",
    "bind_principal_dependency",
    "check_agent_binding",
    # Private internal -- re-exported for tests of
    # the legacy principal-parsing branch only.
    # Production code MUST NOT import this.
    "_legacy_principal",
]
