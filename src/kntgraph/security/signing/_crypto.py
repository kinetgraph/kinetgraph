# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Optional crypto dependency layer.

The signing module requires ``cryptography>=41.0`` and
``canonicaljson>=2.0``. Both are listed under the
``[crypto]`` extra in ``pyproject.toml`` so the framework
can load without them (only ``sign_event`` /
``verify_event`` will fail when called).

This module centralises the ``try/except`` so the rest of
the signing package can import the names unconditionally
(or call :func:`require_crypto` to fail fast).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kntgraph.security.signing._errors import CryptoUnavailableError

if TYPE_CHECKING:
    import canonicaljson as canonicaljson
    from cryptography.exceptions import InvalidSignature as InvalidSignature
    from cryptography.hazmat.primitives import serialization as serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey as Ed25519PublicKey,
    )


try:
    import canonicaljson
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when dep missing
    canonicaljson = None
    InvalidSignature = Exception
    Ed25519PrivateKey = None
    Ed25519PublicKey = None
    serialization = None
    CRYPTOGRAPHY_AVAILABLE = False


def __getattr__(name: str) -> Any:
    """PEP 562 fallback for the missing-crypto path."""
    if name in (
        "canonicaljson",
        "InvalidSignature",
        "Ed25519PrivateKey",
        "Ed25519PublicKey",
        "serialization",
    ):
        return globals().get(name)
    raise AttributeError(f"module has no attribute {name!r}")


def require_crypto() -> None:
    """
    Fail fast if the ``[crypto]`` extra is not installed.

    Used by the public ``sign_event`` / ``verify_event``
    paths so the user sees a clear error message instead
    of an opaque ``AttributeError`` on a ``None`` import.

    Raises:
        CryptoUnavailableError: always when the extra is missing.
    """
    if not CRYPTOGRAPHY_AVAILABLE:
        raise CryptoUnavailableError(
            "cryptography>=41.0 and canonicaljson>=2.0 are required. "
            "Install with: pip install 'kntgraph[crypto]'."
        )
