# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Canonical event bytes (RFC 8785 JCS).

Produces the bytes that are signed/verified. Used internally
by :mod:`_sign` and :mod:`_verify`; not part of the public API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kntgraph.security.signing._crypto import (
    canonicaljson,
    require_crypto,
)

if TYPE_CHECKING:
    from kntgraph.core.event import Event


def canonical_event_bytes(event: "Event") -> bytes:
    """Produce the bytes that are signed: JCS canonical form.

    Specifically:
      - ``event.to_dict()`` produces 9 keys
        (see ``kntgraph/core/event.py:457``).
      - The ``signature`` key is omitted (signatures never
        cover themselves).
      - The result is canonicalised per RFC 8785:
        sorted keys, deterministic number formatting,
        deterministic Unicode normalisation.

    Returns:
        ``bytes`` ready for Ed25519 sign/verify.

    Raises:
        CryptoUnavailableError: if ``canonicaljson`` is not
            installed (i.e. the framework was built without
            the ``[crypto]`` extra).
    """
    require_crypto()
    d = event.to_dict()
    # Drop any existing signature: the bytes we sign never
    # cover the signature itself (re-signing is idempotent).
    d.pop("signature", None)
    return canonicaljson.encode_canonical_json(d)
