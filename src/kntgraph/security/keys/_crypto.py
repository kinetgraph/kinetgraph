# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Optional crypto dependency layer for the keys package.

The keys module requires ``cryptography>=41.0`` for
real Ed25519 keypair generation. When unavailable,
callers fall back to :func:`generate_stub_keypair` (the
PR 0 escape hatch, used in tests that don't exercise
signing).

This module centralises the ``try/except`` so the rest of
the keys package can import the names unconditionally
(or call :func:`require_crypto` to fail fast).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when dep missing
    Ed25519PrivateKey = None  # type: ignore[assignment,misc]
    Ed25519PublicKey = None  # type: ignore[assignment,misc]
    serialization = None  # type: ignore[assignment]
    CRYPTOGRAPHY_AVAILABLE = False


if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


# ---------------------------------------------------------------------------
# Stub key classes (PR 0 fallback)
# ---------------------------------------------------------------------------
#
# When ``cryptography`` is not installed, callers can use
# :func:`generate_stub_keypair` to obtain non-signing key
# shapes that satisfy the ``PublicKey`` / ``PrivateKey``
# Protocols. The stub classes are private to this package —
# they are NOT re-exported from ``__init__.py`` and should
# not be imported directly by consumers. Use
# :func:`generate_stub_keypair` instead.


@dataclass(frozen=True, slots=True)
class _StubPublicKey:
    """Stub public key (PR 0 fallback).

    Used when ``cryptography`` is not installed. The
    ``.bytes`` field is a sha256-derived placeholder; this
    object **cannot** verify real Ed25519 signatures.
    """

    bytes: bytes
    algorithm: str  # "stub-v0"


@dataclass(frozen=True, slots=True)
class _StubPrivateKey:
    """Stub private key (PR 0 fallback).

    Used when ``cryptography`` is not installed. The
    ``.bytes`` field is a random placeholder; this object
    **cannot** produce real Ed25519 signatures.
    """

    bytes: bytes
    algorithm: str  # "stub-v0"


def require_crypto() -> None:
    """
    Fail fast if the ``[crypto]`` extra is not installed.

    Used by :func:`generate_keypair` so the user sees a
    clear error instead of an opaque ``AttributeError``
    on a ``None`` import.

    Raises:
        RuntimeError: always when the extra is missing.
    """
    if not CRYPTOGRAPHY_AVAILABLE:
        raise RuntimeError(
            "cryptography>=41.0.0 is required. "
            "Install with: pip install 'kntgraph[crypto]'. "
            "Use generate_stub_keypair() for a non-signing stub."
        )
