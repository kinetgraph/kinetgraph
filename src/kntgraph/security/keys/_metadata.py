# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
security.keys._metadata -- Static metadata for keypairs.

``KeyMetadata`` is the audit-facing value object for a
keypair (when it was created, when it was retired, the
fingerprint operators see in dashboards). It is
distinct from ``Signature.key_epoch`` (which is a
per-signature value pointing at a specific key
version).

The class lives in :mod:`kntgraph.security.keys`
rather than :mod:`kntgraph.security` directly
because the latter imports :mod:`security.keys` at
module load time. Hosting ``KeyMetadata`` here
breaks the cycle and lets
:func:`kntgraph.security.keys._generate._make_metadata`
import it at the top level.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._types import KeyEpoch


@dataclass(frozen=True, slots=True)
class KeyMetadata:
    """Static metadata about a keypair, surfaced for diagnostics.

    Distinct from ``Signature.key_epoch``: ``KeyMetadata`` is
    about the **key** (when it was created, retired); the
    signature's ``key_epoch`` is the **value** that ties a
    signature to a specific key version.
    """

    agent_id: str
    key_epoch: KeyEpoch
    created_at: str  # ISO-8601; kept as str to avoid datetime imports here
    algorithm: str  # "ed25519-v1" once PR 1 lands; "stub-v0" in PR 0
    public_key_fingerprint: str  # sha256(pubkey_bytes)[:16], hex


__all__ = ["KeyMetadata"]


# Re-export so :mod:`kntgraph.security` can keep its public
# ``KeyMetadata`` name without re-defining the dataclass.
KeyMetadataT = KeyMetadata
