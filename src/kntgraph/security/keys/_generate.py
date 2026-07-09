# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Keypair generation and metadata construction.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from kntgraph.security.keys._crypto import (
    Ed25519PrivateKey,
    _StubPrivateKey,
    _StubPublicKey,
    require_crypto,
)
from kntgraph.security.keys._metadata import KeyMetadata
from kntgraph.security.keys._types import (
    Ed25519PrivateKeyWrapper,
    Ed25519PublicKeyWrapper,
    Keypair,
    KeyEpoch,
    PublicKey,
)


def generate_keypair() -> Keypair:
    """Generate a fresh Ed25519 keypair (PR 1 default).

    Returns:
        ``(Ed25519PrivateKeyWrapper, Ed25519PublicKeyWrapper)``
        on success.

    Raises:
        ``RuntimeError`` if ``cryptography>=41.0`` is not
        installed. Use ``generate_stub_keypair()`` to get a
        non-signing stub (PR 0 fallback, used in tests that
        do not exercise the signature path).
    """
    require_crypto()
    priv_obj = Ed25519PrivateKey.generate()
    pub_obj = priv_obj.public_key()
    return (
        Ed25519PrivateKeyWrapper(_key=priv_obj, algorithm="ed25519-v1"),
        Ed25519PublicKeyWrapper(_key=pub_obj, algorithm="ed25519-v1"),
    )


def generate_stub_keypair() -> Keypair:
    """Generate a non-signing stub keypair (PR 0 fallback).

    The bytes are cryptographically random (suitable for
    unique key identity) but cannot produce or verify a real
    Ed25519 signature. Used in tests that exercise the
    ``KeyRegistry`` API without touching the signature path.

    Do not use in production.
    """
    import secrets

    priv_bytes = secrets.token_bytes(32)
    pub_bytes = hashlib.sha256(priv_bytes).digest()
    return (
        _StubPrivateKey(bytes=priv_bytes, algorithm="stub-v0"),
        _StubPublicKey(bytes=pub_bytes, algorithm="stub-v0"),
    )


def _make_metadata(
    agent_id: str,
    key_epoch: KeyEpoch,
    pub: PublicKey,
) -> KeyMetadata:
    fingerprint = hashlib.sha256(pub.bytes).hexdigest()[:16]
    return KeyMetadata(
        agent_id=agent_id,
        key_epoch=key_epoch,
        created_at=datetime.now(timezone.utc).isoformat(),
        algorithm=pub.algorithm,
        public_key_fingerprint=fingerprint,
    )
