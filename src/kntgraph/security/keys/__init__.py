# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Key management for Zero-Trust Level 1 (ADR-016).

This package provides:

  - :class:`PublicKey` / :class:`PrivateKey` — opaque wrappers
    around cryptographic key material. PR 0 used stdlib stubs
    (sha256-derived); PR 1 wraps real Ed25519 keys from
    ``cryptography.hazmat.primitives.asymmetric.ed25519``.
    Both implementations expose the same surface:
    ``.bytes``, ``.algorithm``, ``.fingerprint()``, ``.is_stub``.
  - :data:`KeyEpoch` — ``NewType``-style ``int`` alias for
    monotonic per-agent epoch counters.
  - :class:`KeyMetadata` — audit-facing value object (lives in
    :mod:`kntgraph.security.keys._metadata` to avoid a
    cycle with :mod:`kntgraph.security`).
  - :class:`RevocationRecord` — frozen dataclass for audit
    trail of revoked keys.
  - :func:`generate_keypair` — produces a fresh keypair.
    PR 1 returns real Ed25519 by default;
    :func:`generate_stub_keypair` is the PR 0 escape hatch
    (used in tests that don't require signing).
  - :class:`InMemoryKeyRegistry` — concrete v1 implementation
    of the ``KeyRegistry`` Protocol defined in
    ``kntgraph.security``.

The key types are intentionally small: this package is
the integration point for any backend (in-process dict,
Vault, AWS KMS, GCP KMS, Azure Key Vault, HSM, TEE).
PR 1 adds the actual signing primitives in
``kntgraph.security.signing``.

Optional dependency: ``cryptography>=41.0`` (see
``pyproject.toml [crypto]``). When unavailable,
:func:`generate_keypair` raises ``RuntimeError`` at call
time; callers fall back to :func:`generate_stub_keypair`
which is always available.

Package layout
--------------

* ``_crypto`` — optional crypto import + ``require_crypto()``
  fail-fast helper.
* ``_types`` — :data:`KeyEpoch`, stub and Ed25519 wrappers,
  public type aliases (:data:`PublicKey`, :data:`PrivateKey`,
  :data:`Keypair`).
* ``_metadata`` — :class:`KeyMetadata` (lives here to break
  the ``security`` ↔ ``security.keys`` cycle that would arise
  if ``KeyMetadata`` stayed in ``kntgraph.security``).
* ``_revocation`` — :class:`RevocationRecord` dataclass.
* ``_generate`` — :func:`generate_keypair`,
  :func:`generate_stub_keypair`, and the private
  :func:`_make_metadata` helper.
* ``_registry`` — :class:`InMemoryKeyRegistry` (concrete
  v1 implementation).
"""

from __future__ import annotations

from kntgraph.security.keys._generate import (
    generate_keypair,
    generate_stub_keypair,
)
from kntgraph.security.keys._metadata import KeyMetadata
from kntgraph.security.keys._registry import InMemoryKeyRegistry
from kntgraph.security.keys._revocation import RevocationRecord
from kntgraph.security.keys._types import (
    Ed25519PrivateKeyWrapper,
    Ed25519PublicKeyWrapper,
    KeyEpoch,
    Keypair,
    PrivateKey,
    PublicKey,
)

__all__ = [
    "Ed25519PrivateKeyWrapper",
    "Ed25519PublicKeyWrapper",
    "InMemoryKeyRegistry",
    "KeyEpoch",
    "KeyMetadata",
    "Keypair",
    "PrivateKey",
    "PublicKey",
    "RevocationRecord",
    "generate_keypair",
    "generate_stub_keypair",
]
