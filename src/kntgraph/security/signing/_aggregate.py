# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Aggregate (batch) signature verification and construction.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from kntgraph.security.signing._canonical import canonical_event_bytes
from kntgraph.security.signing._crypto import (
    CRYPTOGRAPHY_AVAILABLE,
    Ed25519PublicKey,
    InvalidSignature,
)
from kntgraph.security.signing._errors import SignatureError
from kntgraph.security.signing._types import (
    SUPPORTED_ALGORITHMS,
    BatchEntry,
    BatchSignature,
    Signature,
)

if TYPE_CHECKING:
    from kntgraph.core.event import Event

    from . import Ed25519PublicKeyWrapper, KeyRegistry


def verify_aggregate_concat(
    batch: BatchSignature,
    *,
    key_registry: "KeyRegistry | None" = None,
) -> bool:
    """Verify a concat-v1 batch of per-event signatures.

    Returns ``True`` iff **every** entry in the batch verifies:
      1. The per-entry algorithm is in ``SUPPORTED_ALGORITHMS``.
      2. If ``key_registry`` is given, ``(agent_id, key_epoch)``
         is not revoked.
      3. The canonical bytes of the entry's event match the
         signature under the entry's public key.

    **All-or-nothing.** A single failed entry makes the
    whole batch return ``False``. This matches BLS aggregate
    semantics: one bad signature invalidates the aggregate.

    Cost: O(N · Ed25519.verify). For N=50, ~500µs.
    Beyond ~100 entries, switch to BLS12-381 (``alg:
    "bls12-381-v1"``) — not in this ADR.

    **Never raises.** Returns ``False`` on any structural
    or cryptographic failure.
    """
    if batch.alg != "concat-v1":
        return False

    if not CRYPTOGRAPHY_AVAILABLE:
        return False

    return all(
        _verify_entry(entry, key_registry=key_registry) for entry in batch.signatures
    )


def _verify_entry(
    entry: BatchEntry,
    *,
    key_registry: "KeyRegistry | None",
) -> bool:
    """Verify a single ``BatchEntry``.

    Returns ``False`` on any structural / cryptographic
    failure. **Never raises.** Pulled out of
    ``verify_aggregate_concat`` so the loop body stays
    flat (CC ≤ 1) and each step is unit-testable.
    """
    sig = entry.signature
    if sig.alg not in SUPPORTED_ALGORITHMS:
        return False
    if key_registry is not None and _is_revoked(
        sig, event=entry.event, key_registry=key_registry
    ):
        return False
    try:
        bytes_to_verify = canonical_event_bytes(entry.event)
    except Exception:
        return False
    try:
        sig_bytes = base64.urlsafe_b64decode(sig.sig + "=" * (-len(sig.sig) % 4))
    except Exception:
        return False
    raw_pub = getattr(entry.public_key, "_key", entry.public_key)
    if not isinstance(raw_pub, Ed25519PublicKey):
        return False
    try:
        raw_pub.verify(sig_bytes, bytes_to_verify)
    except (InvalidSignature, Exception):  # noqa: BLE001 - intentional
        return False
    return True


def _is_revoked(
    sig: Signature,
    *,
    event: "Event",
    key_registry: "KeyRegistry",
) -> bool:
    """True iff ``(agent_id, key_epoch)`` is revoked. Any
    exception from the registry is treated as "revoked"
    (fail-closed).
    """
    try:
        from kntgraph.security.signing._verify import _epoch

        return key_registry.is_revoked(event.agent_id, _epoch(sig.key_epoch))
    except Exception:
        return True


def aggregate_concat(
    signatures_and_events: list[tuple[Signature, "Event", "Ed25519PublicKeyWrapper"]],
) -> BatchSignature:
    """Build a ``BatchSignature`` (concat-v1) from per-event triples.

    Convenience for callers that have a flat list of
    (signature, event, public_key) tuples and want to wrap
    them into the dispatch shape.

    The input list MUST be non-empty. All entries must share
    the same per-entry algorithm (enforced by the
    ``BatchSignature`` constructor).
    """
    if not signatures_and_events:
        raise SignatureError("aggregate_concat requires >= 1 entry")
    entries = tuple(
        BatchEntry(signature=s, event=e, public_key=p)
        for s, e, p in signatures_and_events
    )
    return BatchSignature(alg="concat-v1", signatures=entries)
