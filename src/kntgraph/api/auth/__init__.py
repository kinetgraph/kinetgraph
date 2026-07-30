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

The Redis binding ``knt:api:keys:<sha256>`` stores a
JSON payload:

    {
        "agent_id":  "tenant-A.agent-1",
        "role":      "agent",        # admin | agent | service
        "tenant_id": "tenant-A",    # null for admin
        "key_id":    "k-2026-06-23-001"
    }

Legacy compatibility (removed in 0.10.0, ADR-017 §7.3):
the verifier no longer accepts plain-string bindings
(pre-ADR-017). Such bindings are now rejected as
``AuthError(kind="malformed", ...)``. Operators with
legacy bindings MUST run
``scripts/migrate_principals.py --apply`` to upgrade
the binding table before upgrading to 0.10.0.

Implementation layout
---------------------

The 506-L monolithic ``auth.py`` was split into a
``_auth/`` sub-package so each file is under the 500-L
guideline (AGENTS.md §3.1):

  - ``_auth._errors`` -- ``AuthError``.
  - ``_auth._verifier`` -- ``APIKeyVerifier`` Protocol
    and ``RedisAPIKeyVerifier`` (the default
    implementation).
  - ``_auth._helpers`` -- the two pure helpers
    (``_digest``, ``_decode``) used by the verifier
    pipeline. The pre-ADR-017 ``_legacy_principal``
    factory was removed in 0.10.0.
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
from .._auth._verifier import APIKeyVerifier, RedisAPIKeyVerifier


__all__ = [
    "APIKeyVerifier",
    "AuthError",
    "RedisAPIKeyVerifier",
    "bind_principal_dependency",
    "check_agent_binding",
]
