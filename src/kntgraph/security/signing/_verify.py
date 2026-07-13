# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Verify a single-event signature.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Optional, cast

from kntgraph.security.keys._types import KeyEpoch
from kntgraph.security.signing._canonical import canonical_event_bytes
from kntgraph.security.signing._crypto import (
    CRYPTOGRAPHY_AVAILABLE,
    Ed25519PublicKey,
)
from kntgraph.security.signing._types import SUPPORTED_ALGORITHMS, Signature

if TYPE_CHECKING:
    from kntgraph.core.event import Event
    from kntgraph.security import Ed25519PublicKeyWrapper, KeyRegistry


def _epoch(value: int) -> "KeyEpoch":
    """Coerce an int into a ``KeyEpoch`` (NewType)."""
    return KeyEpoch(value)


def verify_event(
    event: "Event",
    public_key: "Ed25519PublicKeyWrapper",
    *,
    key_registry: "Optional[KeyRegistry]" = None,
) -> bool:
    """Verify an event's signature against a public key.

    Returns ``True`` iff all checks pass:
      1. ``event.signature`` is present.
      2. ``Signature.alg`` is in ``SUPPORTED_ALGORITHMS``.
      3. The bytes re-canonicalised from ``event.to_dict()``
         (with ``signature`` omitted) match the signature
         under the public key.
      4. If ``key_registry`` is provided, ``(agent_id,
         key_epoch)`` is **not** in the revocation list.

    **This function never raises.** Returns ``False`` for
    any failure (unknown algorithm, bad signature, revoked
    key, missing signature). Callers that need a
    structured error should use ``verify_event_verbose``
    (PR 3, not yet shipped) or wrap the boolean in their
    own error model.

    Args:
        event: The event to verify.
        public_key: An ``Ed25519PublicKeyWrapper`` or any
            object exposing ``._key`` of type
            ``cryptography...Ed25519PublicKey``.
        key_registry: Optional. When provided, the
            ``(agent_id, signature.key_epoch)`` pair is
            checked against the revocation list. Revoked
            keys cause ``False`` (not raise).
    """
    sig = getattr(event, "signature", None)
    if sig is None:
        return False
    if sig.alg not in SUPPORTED_ALGORITHMS:
        return False
    if key_registry is not None and _is_revoked(
        event, sig=sig, key_registry=key_registry
    ):
        return False
    if not CRYPTOGRAPHY_AVAILABLE:
        # No crypto means we can't actually verify; fail
        # closed (return False). Callers must install the
        # [crypto] extra for verification.
        return False
    return _crypto_verify(event, sig, public_key)


def _is_revoked(
    event: "Event",
    *,
    sig: "Signature",
    key_registry: "KeyRegistry",
) -> bool:
    """True iff ``(agent_id, key_epoch)`` is revoked. Any
    exception from the registry is treated as "revoked"
    (fail-closed).
    """
    try:
        return key_registry.is_revoked(event.agent_id, _epoch(sig.key_epoch))
    except Exception:
        return True


def _crypto_verify(
    event: "Event",
    sig: "Signature",
    public_key: "Ed25519PublicKeyWrapper",
) -> bool:
    """Verify the Ed25519 signature on the canonical
    bytes of ``event``. **Never raises** — any
    exception is mapped to ``False``.

    Pulled out of ``verify_event`` so the orchestrator
    stays flat (CC ≤ 3) and each step is unit-testable.
    """
    try:
        bytes_to_verify = canonical_event_bytes(event)
    except Exception:
        return False

    try:
        sig_bytes = base64.urlsafe_b64decode(sig.sig + "=" * (-len(sig.sig) % 4))
    except Exception:
        return False

    raw_pub = cast(
        Ed25519PublicKey,
        getattr(public_key, "_key", public_key),
    )
    if not hasattr(raw_pub, "verify"):
        return False

    try:
        raw_pub.verify(sig_bytes, bytes_to_verify)
    except Exception:  # noqa: BLE001 - intentional
        return False
    return True
