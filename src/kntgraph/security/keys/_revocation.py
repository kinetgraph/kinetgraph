# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Revocation record dataclass for the keys package.

Audit-trail entry for revoked keys, stored in
``InMemoryKeyRegistry._revoked`` and (in L2) in the
``knt:revocations:{agent_id}`` Redis Stream for
cross-verifier propagation.
"""

from __future__ import annotations

from dataclasses import dataclass

from kntgraph.security.keys._types import KeyEpoch


@dataclass(frozen=True, slots=True)
class RevocationRecord:
    """Audit-trail entry for a revoked key.

    Stored in ``InMemoryKeyRegistry._revoked`` and (in L2) in
    the ``knt:revocations:{agent_id}`` Redis Stream for
    cross-verifier propagation.
    """

    agent_id: str
    key_epoch: KeyEpoch
    reason: str
    revoked_at: str  # ISO-8601 UTC
    revoked_by: str  # operator id; "system" for automated revocations
