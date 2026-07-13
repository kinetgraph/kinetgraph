# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Cryptographic key types for the keys package.

Contains:

* :data:`KeyEpoch` — ``NewType``-style ``int`` alias for monotonic
  per-agent epoch counters.
* :class:`Ed25519PublicKeyWrapper` / :class:`Ed25519PrivateKeyWrapper`
  — PR 1 default (real Ed25519 from
  ``cryptography.hazmat.primitives.asymmetric.ed25519``).
* :data:`PublicKey` / :data:`PrivateKey` / :data:`Keypair` —
  public union type aliases over the above.

The stub key classes (``_StubPublicKey`` / ``_StubPrivateKey``)
live in :mod:`kntgraph.security.keys._crypto` next to the
optional crypto import — they are PR 0 fallbacks for when
``cryptography`` is not installed and are NOT part of the
public API.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import NewType, Union

from kntgraph.security.keys._crypto import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
    _StubPrivateKey,
    _StubPublicKey,
    serialization,
)


KeyEpoch = NewType("KeyEpoch", int)
"""Monotonic epoch counter per ``agent_id``.

``KeyEpoch(0)`` is the first registered keypair; subsequent
registrations return ``1``, ``2``, ... in order. A revoke
does **not** consume an epoch (revoked keys remain in
``_revoked`` for historical verification).
"""


@dataclass(frozen=True, slots=True)
class Ed25519PublicKeyWrapper:
    """Real Ed25519 public key (PR 1 default).

    Wraps ``cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey``.
    The internal object is never exposed; consumers interact
    only with ``.bytes``, ``.algorithm``, ``.fingerprint()``.
    """

    _key: Ed25519PublicKey
    algorithm: str  # "ed25519-v1"

    @property
    def bytes(self) -> bytes:
        """Raw 32-byte Ed25519 public key."""
        return self._key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def fingerprint(self) -> str:
        """sha256(pubkey)[:16], hex. Used by audit dashboards."""
        return hashlib.sha256(self.bytes).hexdigest()[:16]

    @property
    def is_stub(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class Ed25519PrivateKeyWrapper:
    """Real Ed25519 private key (PR 1 default).

    Wraps ``cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey``.
    The internal object is never exposed; consumers interact
    only with ``.bytes``, ``.algorithm``, ``.fingerprint()``,
    ``.public_key()``.

    SECURITY: ``.bytes`` exposes the raw 32-byte private seed.
    Production code must never log, persist unencrypted, or
    transmit this value. Use ``.public_key()`` for any
    cross-process need.
    """

    _key: Ed25519PrivateKey
    algorithm: str  # "ed25519-v1"

    @property
    def bytes(self) -> bytes:
        """Raw 32-byte Ed25519 private seed (SECURITY-SENSITIVE)."""
        return self._key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def fingerprint(self) -> str:
        """sha256(pubkey)[:16], hex. The public fingerprint."""
        return self.public_key().fingerprint()

    def public_key(self) -> Ed25519PublicKeyWrapper:
        """Derive the matching public key (cheap, deterministic)."""
        pub = self._key.public_key()
        return Ed25519PublicKeyWrapper(_key=pub, algorithm=self.algorithm)

    @property
    def is_stub(self) -> bool:
        return False


# Public type aliases. The Protocol accepts anything with
# ``.bytes`` and ``.algorithm`` (duck-typed for backwards
# compatibility with PR 0 stubs). PR 1 prefers the wrappers.
PublicKey = Union[Ed25519PublicKeyWrapper, _StubPublicKey]
PrivateKey = Union[Ed25519PrivateKeyWrapper, _StubPrivateKey]
Keypair = tuple[PrivateKey, PublicKey]
