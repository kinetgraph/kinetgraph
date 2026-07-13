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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cryptography.hazmat.primitives import serialization as serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey as Ed25519PublicKey,
    )


try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when dep missing
    Ed25519PrivateKey = None
    Ed25519PublicKey = None
    serialization = None
    CRYPTOGRAPHY_AVAILABLE = False


def __getattr__(name: str) -> Any:
    """
    PEP 562: when the optional ``cryptography`` dep is
    missing, the names are ``None`` at runtime. Returning
    the module attribute here keeps downstream imports
    (and pyright) happy: callers should always
    :func:`require_crypto` first, so the ``None`` branch
    is a hard failure on the first crypto call.
    """
    if name in ("Ed25519PrivateKey", "Ed25519PublicKey", "serialization"):
        return globals().get(name)
    raise AttributeError(
        f"module 'kntgraph.security.keys._crypto' has no attribute {name!r}"
    )


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
