# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.security — Zero-Trust Levels 1 and 2.

Level 1 (ADR-016):
  - ``KeyRegistry`` — Protocol for resolving ``agent_id``
    to public/private keys. v1 ships ``InMemoryKeyRegistry``
    (dict-backed, lost on restart). v2 plugs in Vault/KMS
    via the same Protocol.
  - ``Keypair``, ``PublicKey``, ``PrivateKey`` — opaque
    wrappers over key material. PR 1 uses real Ed25519
    from ``cryptography``; PR 0 stubs (sha256-derived)
    remain available via ``generate_stub_keypair()`` for
    tests that don't exercise signing.
  - ``generate_keypair()`` — convenience that returns a
    fresh (private, public) Ed25519 keypair (PR 1).
  - ``generate_stub_keypair()`` — PR 0 stub keypair
    (no signing capability; use in unit tests).
  - ``KeyEpoch`` — monotonic counter per ``agent_id`` for
    revocation (L2); threaded through ``Signature.key_epoch``.
  - ``RevocationRecord`` — opaque value type stored in
    ``KeyRegistry._revoked``.

PR 1 (``signing.py``) adds ``Signature``,
``canonical_event_bytes``, ``sign_event`` and ``verify_event``.
The Protocol intentionally does NOT include signing/verify
methods; those live in the dedicated module so that
``KeyRegistry`` can be implemented by HSM-backed backends
that never expose the private key.

Level 2 (ADR-017):
  - ``Principal`` — immutable identity record
    (agent_id + role + tenant_id + key_id).
  - ``Role`` — admin | agent | service.
  - ``Action`` and ``Resource`` — the policy contract.
  - ``Policy`` — the authorisation evaluator
    (always-allow vs default).
  - ``principal_ctx`` — ``ContextVar`` populated by the
    request middleware and read by ``EventLog.append`` and
    ``ToolInvoker`` to attribute operations.

Optional dependency: ``cryptography>=41.0`` (see
``pyproject.toml [crypto]``). When unavailable, key
generation raises ``RuntimeError``; stub mode is opt-in.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .keys import (
    Ed25519PrivateKeyWrapper,
    Ed25519PublicKeyWrapper,
    InMemoryKeyRegistry,
    KeyEpoch,
    KeyMetadata,
    Keypair,
    PrivateKey,
    PublicKey,
    RevocationRecord,
    generate_keypair,
    generate_stub_keypair,
)
from .principal import (
    Action,
    AlwaysAllowPolicy,
    DefaultPolicy,
    Principal,
    Resource,
    Role,
    principal_ctx,
)
from .signing import (
    SUPPORTED_ALGORITHMS,
    SUPPORTED_BATCH_ALGORITHMS,
    BatchEntry,
    BatchSignature,
    CryptoUnavailableError,
    Signature,
    SignatureError,
    UnknownAlgorithmError,
    aggregate_concat,
    canonical_event_bytes,
    sign_event,
    verify_aggregate_concat,
    verify_event,
)


@runtime_checkable
class KeyRegistry(Protocol):
    """Resolves ``agent_id`` to (public key, private key).

    v1 implementation: ``InMemoryKeyRegistry`` (in-process
    dict). Keys are lost on process restart; callers that
    need durability must persist the PEM-encoded key and
    re-hydrate at boot (10-line utility, not in this ADR).

    v2 implementations: ``VaultKeyRegistry`` / ``KmsKeyRegistry``
    plug in via the same Protocol. Call sites that take a
    ``KeyRegistry`` (e.g. ``EventLog`` in PR 3) do not change.

    The Protocol is ``runtime_checkable`` so test code can use
    ``isinstance(reg, KeyRegistry)`` to validate mock objects
    without subclassing.
    """

    def public_key(
        self,
        agent_id: str,
        key_epoch: KeyEpoch = KeyEpoch(0),
    ) -> PublicKey:
        """Return the public key for ``agent_id`` at ``key_epoch``.

        Default ``key_epoch=0`` is the current epoch. Searches
        retired keys if the epoch is no longer current (L3
        long-term key history).
        """
        ...

    def private_key(self, agent_id: str) -> PrivateKey:
        """Return the **current** private key for ``agent_id``.

        Only available to the producer side (never to verifiers).
        Implementations are expected to enforce authorisation
        at the boundary (e.g. Vault transit endpoint); the
        in-memory impl simply looks up the dict.
        """
        ...

    def register(
        self,
        agent_id: str,
        priv: PrivateKey,
    ) -> KeyEpoch:
        """Register a new keypair; return the assigned epoch.

        The first registration returns epoch ``0``; subsequent
        ones return ``1``, ``2``, ... monotonically. A revogação
        (see ``revoke``) does **not** consume an epoch.
        """
        ...

    def revoke(
        self,
        agent_id: str,
        key_epoch: KeyEpoch,
        reason: str,
    ) -> RevocationRecord:
        """Mark ``(agent_id, key_epoch)`` as revoked.

        Future signatures under that key fail ``verify_event``
        (PR 1). Past signatures under that key **continue to
        verify** (non-repudiation of historical events).
        """
        ...

    def is_revoked(
        self,
        agent_id: str,
        key_epoch: KeyEpoch,
    ) -> bool:
        """Whether ``(agent_id, key_epoch)`` is revoked.

        Cheap: O(1) dict lookup in the in-process impl.
        """
        ...

    def current_epoch(self, agent_id: str) -> KeyEpoch:
        """Current (non-revoked) epoch for ``agent_id``.

        Used by verifiers that want to fast-path the
        "is this signature from the current key?" check
        without walking the revocation list.
        """
        ...

    def metadata(self, agent_id: str, key_epoch: KeyEpoch) -> KeyMetadata:
        """Static metadata for diagnostics / audit dashboards."""
        ...


__all__ = [
    "Action",
    "AlwaysAllowPolicy",
    "BatchEntry",
    "BatchSignature",
    "CryptoUnavailableError",
    "DefaultPolicy",
    "Ed25519PrivateKeyWrapper",
    "Ed25519PublicKeyWrapper",
    "InMemoryKeyRegistry",
    "KeyEpoch",
    "KeyMetadata",
    "KeyRegistry",
    "Keypair",
    "Policy",
    "Principal",
    "PrivateKey",
    "PublicKey",
    "Resource",
    "RevocationRecord",
    "Role",
    "SUPPORTED_ALGORITHMS",
    "SUPPORTED_BATCH_ALGORITHMS",
    "Signature",
    "SignatureError",
    "UnknownAlgorithmError",
    "aggregate_concat",
    "canonical_event_bytes",
    "generate_keypair",
    "generate_stub_keypair",
    "principal_ctx",
    "sign_event",
    "verify_aggregate_concat",
    "verify_event",
]
