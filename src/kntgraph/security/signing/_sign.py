# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Sign an event with an Ed25519 private key.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from typing import TYPE_CHECKING

from kntgraph.security.signing._canonical import canonical_event_bytes
from kntgraph.security.signing._crypto import (
    Ed25519PrivateKey,
    require_crypto,
    serialization,
)
from kntgraph.security.signing._errors import SignatureError
from kntgraph.security.signing._types import Signature

if TYPE_CHECKING:
    from kntgraph.core.event import Event

    from . import Ed25519PrivateKeyWrapper


def sign_event(event: "Event", private_key: "Ed25519PrivateKeyWrapper") -> "Event":
    """Sign an event with the given private key.

    The input event is **not mutated** (it is a frozen
    dataclass). A new ``Event`` is returned with
    ``event.signature`` populated.

    Args:
        event: The event to sign.
        private_key: An ``Ed25519PrivateKeyWrapper`` (from
            ``kntgraph.security.keys``) or any object
            that exposes ``._key`` of type
            ``cryptography...Ed25519PrivateKey``.

    Returns:
        A new ``Event`` with the same fields plus a
        ``Signature`` attached.

    Raises:
        CryptoUnavailableError: if ``cryptography`` is not
            installed.
        SignatureError: on encoding / algorithm issues
        (validation in ``Signature.__post_init__``).
    """
    require_crypto()

    # Extract the underlying cryptography object.
    raw_priv = getattr(private_key, "_key", private_key)
    if not isinstance(raw_priv, Ed25519PrivateKey):
        raise SignatureError(
            f"sign_event requires an Ed25519PrivateKey, got {type(raw_priv).__name__}"
        )

    bytes_to_sign = canonical_event_bytes(event)
    sig_bytes = raw_priv.sign(bytes_to_sign)

    # Derive the public key for the signature payload.
    raw_pub = raw_priv.public_key()
    pk_bytes = raw_pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    sig = Signature(
        alg="ed25519-v1",
        pk=base64.urlsafe_b64encode(pk_bytes).rstrip(b"=").decode("ascii"),
        sig=base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode("ascii"),
        key_epoch=0,
    )

    return replace(event, signature=sig)
