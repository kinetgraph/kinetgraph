# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Hashing helpers.

Centralises the SHA-256-truncated pattern that was
duplicated 7+ times across the codebase:

    hashlib.sha256(...).hexdigest()[:16]

The 16-char truncation gives 64 bits of entropy —
enough for in-process cache keys, fingerprint
identifiers, and idempotency slots. NOT a security
primitive; for cryptographic key fingerprints
(e.g. `security/keys.py:Key.fingerprint`), the full
hex digest is used.

The helper accepts either `str` (UTF-8 encoded
internally) or `bytes`. Mixing the two is a
footgun the old pattern invited (every call site
re-encoded); the new helper is explicit.
"""

from __future__ import annotations

import hashlib
from typing import Union


# Default truncation length: 16 hex chars = 64 bits.
DEFAULT_HASH_LEN: int = 16


def short_hash(
    data: Union[str, bytes],
    *,
    length: int = DEFAULT_HASH_LEN,
) -> str:
    """
    SHA-256 of `data`, truncated to `length` hex chars.

    Args:
        data: The data to hash. Strings are UTF-8 encoded.
        length: Truncation length in hex chars
            (default 16 = 64 bits). The full digest
            is 64 chars; values > 64 return the full
            digest.

    Returns:
        The hex digest, truncated.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    if length >= len(digest):
        return digest
    return digest[:length]


__all__ = [
    "DEFAULT_HASH_LEN",
    "short_hash",
]
