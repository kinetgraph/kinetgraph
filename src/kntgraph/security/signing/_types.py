# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Cryptographic dataclasses for the signing package.

Contains:

* :data:`SUPPORTED_ALGORITHMS` — whitelist of single-event algorithms.
* :data:`SUPPORTED_BATCH_ALGORITHMS` — whitelist of batch algorithms.
* :class:`Signature` — single-event signature (frozen dataclass).
* :class:`BatchSignature` — linear concat of per-event signatures.
* :class:`BatchEntry` — one ``(signature, event, public_key)`` triple.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kntgraph.core._typing import JsonValue
from kntgraph.security.signing._errors import (
    SignatureError,
    UnknownAlgorithmError,
)

if TYPE_CHECKING:
    from kntgraph.core.event import Event
    from kntgraph.security import Ed25519PublicKeyWrapper


SUPPORTED_ALGORITHMS: frozenset[str] = frozenset({"ed25519-v1"})
"""Algorithms this build can sign with.

The whitelist is enforced at ``Signature`` creation
(unknown algorithm raises ``UnknownAlgorithmError``).
The verifier dispatch checks the same set; anything
outside returns ``False``.

Future versions add ``ecdsa-p256-sha256-v1``,
``bls12-381-v1``, ``ml-dsa-65-v1`` (see ADR-016 §4.5).
"""


SUPPORTED_BATCH_ALGORITHMS: frozenset[str] = frozenset({"concat-v1"})
"""Batch signature algorithms this build supports.

v1 (this PR): ``concat-v1`` — linear concatenation of N
per-event ``Signature`` objects. Verification is O(N · verify)
per signature in the batch. Acceptable when N ≤ ~50; above
that, switch to BLS12-381 (v2, not in this ADR).

The whitelist is enforced at ``BatchSignature`` creation;
unknown algorithms raise ``UnknownAlgorithmError``. The
verifier returns ``False`` for unknown batch algorithms.
"""


@dataclass(frozen=True, slots=True)
class Signature:
    """Cryptographic signature on a single event.

    Covers the JCS-canonical bytes of the event's ``to_dict()``
    with the ``signature`` field absent (so re-signing is
    idempotent — the signature does not cover itself).

    Attributes:
        alg: Algorithm tag, e.g. ``"ed25519-v1"``. Versioned
            so future migrations are non-breaking.
        pk: Base64 (no padding) of the raw 32-byte public key.
        sig: Base64 (no padding) of the 64-byte Ed25519
            signature.
        key_epoch: Monotonic per-``agent_id`` epoch. Set to
            ``KeyEpoch(0)`` for the first key; PR 2 (revocation)
            checks this against the registry's revocation list.
    """

    alg: str
    pk: str
    sig: str
    key_epoch: int = 0

    def __post_init__(self) -> None:
        if self.alg not in SUPPORTED_ALGORITHMS:
            raise UnknownAlgorithmError(self.alg)
        # Validate base64 shapes early (cheap, fails fast).
        try:
            pk_bytes = base64.urlsafe_b64decode(self.pk + "=" * (-len(self.pk) % 4))
        except Exception as exc:
            raise SignatureError(f"pk is not valid base64url: {exc}") from exc
        try:
            sig_bytes = base64.urlsafe_b64decode(self.sig + "=" * (-len(self.sig) % 4))
        except Exception as exc:
            raise SignatureError(f"sig is not valid base64url: {exc}") from exc
        # Length checks per algorithm.
        if self.alg == "ed25519-v1":
            if len(pk_bytes) != 32:
                raise SignatureError(
                    f"ed25519-v1 requires 32-byte pk, got {len(pk_bytes)}"
                )
            if len(sig_bytes) != 64:
                raise SignatureError(
                    f"ed25519-v1 requires 64-byte sig, got {len(sig_bytes)}"
                )

    # -- serialisation ------------------------------------------------

    def to_dict(self) -> dict[str, JsonValue]:
        """Serialise to a JSON-friendly dict (JCS-friendly)."""
        return {
            "alg": self.alg,
            "pk": self.pk,
            "sig": self.sig,
            "key_epoch": self.key_epoch,
        }

    @classmethod
    def from_dict(cls, d: dict[str, JsonValue]) -> "Signature":
        """Inverse of ``to_dict``. Tolerates missing key_epoch (=0)."""
        return cls(
            alg=_scalar(d.get("alg")),
            pk=_scalar(d.get("pk")),
            sig=_scalar(d.get("sig")),
            key_epoch=int(d.get("key_epoch", 0) or 0),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class BatchSignature:
    """Linear concatenation of N per-event signatures (ADR-016 §4.4).

    This is **not** a true aggregate signature: each entry
    is an independent ``Signature`` that verifies against
    one event under one public key. The batch is a tuple;
    no aggregation math is performed.

    Why keep this shape:
      - v1 cost: O(N · Ed25519.verify) ≈ O(N · 10µs).
      - v2 replacement: BLS12-381 aggregate (single 96-byte
        signature, 1 pairing check, ~2.7 ms for any N).
      - Forward compat: a future ``AggregateSignature``
        with ``alg="bls12-381-v1"`` slots into the same
        dispatch site (``verify_aggregate_concat`` is the
        generic name; PR 4 ships the concat implementation
        and the dispatch skeleton).

    Properties:
      - All signatures in a batch MUST use the same
        algorithm (``alg``). Mixing is rejected.
      - Each signature covers one event; the batch is just
        a (signature, event, public_key) triple collection.
      - For revocation: each entry is checked individually
        against the registry (no batch revocation).
    """

    alg: str  # "concat-v1" enforced
    signatures: tuple["BatchEntry", ...]

    def __post_init__(self) -> None:
        if not self.signatures:
            raise SignatureError("BatchSignature requires >= 1 entry")
        if self.alg not in SUPPORTED_BATCH_ALGORITHMS:
            raise UnknownAlgorithmError(self.alg)
        # All entries must use the same individual algorithm.
        algs = {entry.signature.alg for entry in self.signatures}
        if len(algs) > 1:
            raise SignatureError(f"BatchSignature mixes per-entry algorithms: {algs}")


@dataclass(frozen=True, slots=True)
class BatchEntry:
    """One entry in a ``BatchSignature``.

    Pairs a per-event ``Signature`` with the event it covers
    and the public key it verifies against. The signature
    alone is not enough to verify — the verifier needs the
    bytes that were signed (the event) and the public key
    that should sign them.
    """

    signature: Signature
    event: "Event"  # forward ref via TYPE_CHECKING (avoids cycle)
    public_key: "Ed25519PublicKeyWrapper"  # compatible with any
    # object that exposes ``verify(signature, message)``
    # (see ``kntgraph.security.keys``).


def _scalar(value: JsonValue) -> str:
    """Coerce a ``JsonValue`` slot to ``str``. Returns
    ``""`` for None or non-scalar shapes.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""
