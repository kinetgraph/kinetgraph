# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Sign and verify events (ADR-016 PR 1).

This package provides:

  - :class:`Signature` — frozen dataclass carrying the algorithm
    tag, base64-encoded public key, base64-encoded signature,
    and key epoch. The wire shape is designed to be
    interoperable: any RFC 8785 implementation + any
    Ed25519 implementation can verify a signature produced
    here (and vice-versa).

  - :func:`canonical_event_bytes` — produces the bytes
    that are signed: the JCS (RFC 8785) canonical form of
    ``event.to_dict()`` with the ``signature`` field omitted.
    Deterministic across Python versions and (in principle)
    across languages.

  - :func:`sign_event` — produces an
    ``Event`` with ``event.signature`` populated. The
    original event is not mutated (frozen dataclass); a
    new event is returned.

  - :func:`verify_event` — returns ``True`` iff the signature
    is valid for the given public key. **Never raises.**
    Returns ``False`` for: unknown algorithm, missing
    signature, wrong bytes, wrong key, revoked key (when
    ``registry`` is provided).

  - :class:`BatchSignature` — placeholder for the concat-v1
    aggregator (PR 4 ships the real version).

The optional dependency is ``cryptography>=41.0`` and
``canonicaljson>=2.0`` (see ``pyproject.toml [crypto]``).
When unavailable, :func:`sign_event` and :func:`verify_event`
raise :class:`CryptoUnavailableError` at call time, NOT at
import time. This lets the framework load without the crypto
extra; calling code that needs signing pays the cost on
first use.

Algorithm agility: ``Signature.alg`` carries the versioned
contract (``"ed25519-v1"``). Unknown algorithms are
**rejected** by :func:`sign_event` (whitelist enforced at
creation) and **return False** from :func:`verify_event`
(a verifier that does not know the algorithm cannot make
a trust decision).

See ``kntgraph/docs/security/signing.md`` for the
operational contract; ``ADR-016`` for the design record.

Package layout
--------------

* ``_types`` — frozen dataclasses (``Signature``,
  ``BatchSignature``, ``BatchEntry``) + algorithm whitelists.
* ``_errors`` — exception types (``SignatureError`` and
  subclasses).
* ``_crypto`` — optional crypto import + ``require_crypto()``
  fail-fast helper.
* ``_canonical`` — :func:`canonical_event_bytes` (internal).
* ``_sign`` — :func:`sign_event`.
* ``_verify`` — :func:`verify_event` + the private
  :func:`_epoch` helper.
* ``_aggregate`` — :func:`verify_aggregate_concat` and
  :func:`aggregate_concat`.
"""

from __future__ import annotations

from kntgraph.security.signing._aggregate import (
    aggregate_concat,
    verify_aggregate_concat,
)
from kntgraph.security.signing._errors import (
    CryptoUnavailableError,
    SignatureError,
    UnknownAlgorithmError,
)
from kntgraph.security.signing._sign import sign_event
from kntgraph.security.signing._types import (
    SUPPORTED_ALGORITHMS,
    SUPPORTED_BATCH_ALGORITHMS,
    BatchEntry,
    BatchSignature,
    Signature,
)
from kntgraph.security.signing._verify import verify_event

__all__ = [
    "BatchEntry",
    "BatchSignature",
    "CryptoUnavailableError",
    "Signature",
    "SignatureError",
    "SUPPORTED_ALGORITHMS",
    "SUPPORTED_BATCH_ALGORITHMS",
    "UnknownAlgorithmError",
    "aggregate_concat",
    "canonical_event_bytes",
    "sign_event",
    "verify_aggregate_concat",
    "verify_event",
]

# Re-export ``canonical_event_bytes`` from its module so it
# remains part of the public API surface (it is used by
# callers that want to inspect the bytes without
# round-tripping through sign/verify).
from kntgraph.security.signing._canonical import canonical_event_bytes  # noqa: E402
