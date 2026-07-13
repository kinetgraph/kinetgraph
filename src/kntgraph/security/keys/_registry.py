# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
In-memory implementation of the ``KeyRegistry`` Protocol.

The v1 implementation. Keys are lost on process restart;
callers that need durability must persist the PEM-encoded
key and re-hydrate at boot. The class does not provide
``load_pem`` / ``dump_pem`` helpers in PR 0 (PR 1 will
add them when ``cryptography`` is wired).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from kntgraph.security.keys._crypto import (
    _StubPrivateKey,
    _StubPublicKey,
)
from kntgraph.security.keys._generate import _make_metadata
from kntgraph.security.keys._revocation import RevocationRecord
from kntgraph.security.keys._types import (
    Ed25519PrivateKeyWrapper,
    KeyEpoch,
    PrivateKey,
    PublicKey,
)


class InMemoryKeyRegistry:
    """Concrete ``KeyRegistry`` for development and tests.

    Storage:
      - ``_keys: dict[(agent_id, KeyEpoch), (priv, pub)]`` —
        full key history, including retired epochs.
      - ``_current: dict[agent_id, KeyEpoch]`` — the highest
        non-revoked epoch per agent.
      - ``_revoked: dict[(agent_id, KeyEpoch), RevocationRecord]``
        — revocation list.

    Concurrency: this class is **not** thread-safe by itself.
    Wrap with a lock if shared across threads. Async safety
    is the caller's responsibility (a single coroutine
    sequence is fine; concurrent ``register``/``revoke`` from
    multiple coroutines needs an external lock).

    v1 limitation: keys are lost on process restart. Callers
    that need durability must persist the PEM-encoded key
    and re-hydrate at boot. The ``InMemoryKeyRegistry`` does
    not provide ``load_pem`` / ``dump_pem`` helpers in PR 0
    (PR 1 will add them when ``cryptography`` is wired).
    """

    __slots__ = ("_keys", "_current", "_revoked", "_metadata", "_revoked_seq")

    def __init__(self) -> None:
        self._keys: dict[tuple[str, KeyEpoch], tuple[PrivateKey, PublicKey]] = {}
        self._current: dict[str, KeyEpoch] = {}
        self._revoked: dict[tuple[str, KeyEpoch], RevocationRecord] = {}
        self._metadata: dict[tuple[str, KeyEpoch], "object"] = {}
        self._revoked_seq: int = 0

    # -- read ------------------------------------------------------------

    def public_key(
        self,
        agent_id: str,
        key_epoch: KeyEpoch = KeyEpoch(0),
    ) -> PublicKey:
        if (agent_id, key_epoch) not in self._keys:
            raise KeyError(
                f"no public key for agent_id={agent_id!r} key_epoch={key_epoch!r}"
            )
        return self._keys[(agent_id, key_epoch)][1]

    def private_key(self, agent_id: str) -> PrivateKey:
        epoch = self._current.get(agent_id)
        if epoch is None:
            raise KeyError(f"no current private key for agent_id={agent_id!r}")
        return self._keys[(agent_id, epoch)][0]

    def current_epoch(self, agent_id: str) -> KeyEpoch:
        epoch = self._current.get(agent_id)
        if epoch is None:
            raise KeyError(f"no current epoch for agent_id={agent_id!r}")
        return epoch

    def is_revoked(self, agent_id: str, key_epoch: KeyEpoch) -> bool:
        return (agent_id, key_epoch) in self._revoked

    def metadata(self, agent_id: str, key_epoch: KeyEpoch) -> "object":
        meta = self._metadata.get((agent_id, key_epoch))
        if meta is None:
            raise KeyError(
                f"no metadata for agent_id={agent_id!r} key_epoch={key_epoch!r}"
            )
        return meta

    def revoked_keys(self, agent_id: str) -> list[tuple[KeyEpoch, RevocationRecord]]:
        """List all revoked epochs for an ``agent_id`` (L3 audit use)."""
        return [
            (epoch, rec)
            for (aid, epoch), rec in self._revoked.items()
            if aid == agent_id
        ]

    # -- write -----------------------------------------------------------

    def register(
        self,
        agent_id: str,
        priv: PrivateKey,
    ) -> KeyEpoch:
        """Register a new keypair; return the assigned epoch.

        Idempotency: registering the same private key twice
        (same ``.bytes``) returns the existing epoch rather
        than allocating a new one. This protects against
        accidental double-registration at boot.

        Accepts both Ed25519 wrappers and PR 0 stubs. The
        matching public key is derived deterministically:
        Ed25519 via ``priv.public_key()``, stub via
        ``sha256(priv.bytes)``.
        """
        for (aid, epoch), (existing_priv, _) in self._keys.items():
            if aid == agent_id and existing_priv.bytes == priv.bytes:
                return epoch

        # Derive the public key. Two branches: real Ed25519
        # (deterministic from the private object) and stub
        # (sha256 of the bytes).
        if isinstance(priv, Ed25519PrivateKeyWrapper):
            pub = priv.public_key()
        elif isinstance(priv, _StubPrivateKey):
            pub = _StubPublicKey(
                bytes=hashlib.sha256(priv.bytes).digest(),  # noqa: S324 - non-crypto use
                algorithm=priv.algorithm,
            )
        else:  # pragma: no cover - defensive
            raise TypeError(f"unsupported private key type: {type(priv).__name__}")

        # Allocate next epoch. Starting from 0; monotonic.
        next_epoch_int = 0
        if agent_id in self._current:
            next_epoch_int = int(self._current[agent_id]) + 1
        next_epoch = KeyEpoch(next_epoch_int)

        self._keys[(agent_id, next_epoch)] = (priv, pub)
        self._current[agent_id] = next_epoch

        # Metadata. The fingerprint is the public-key hex
        # prefix; useful for log correlation and audit dashboards.
        self._metadata[(agent_id, next_epoch)] = _make_metadata(
            agent_id, next_epoch, pub
        )
        return next_epoch

    def revoke(
        self,
        agent_id: str,
        key_epoch: KeyEpoch,
        reason: str,
        revoked_by: str = "system",
    ) -> RevocationRecord:
        if (agent_id, key_epoch) not in self._keys:
            raise KeyError(
                f"cannot revoke unknown key: agent_id={agent_id!r} "
                f"key_epoch={key_epoch!r}"
            )
        if self.is_revoked(agent_id, key_epoch):
            # Idempotent: re-revoking returns the existing record.
            return self._revoked[(agent_id, key_epoch)]

        rec = RevocationRecord(
            agent_id=agent_id,
            key_epoch=key_epoch,
            reason=reason,
            revoked_at=datetime.now(timezone.utc).isoformat(),
            revoked_by=revoked_by,
        )
        self._revoked[(agent_id, key_epoch)] = rec
        self._revoked_seq += 1

        # If the current epoch is the revoked one, advance.
        # (A revoked current key has no current "live" key
        # until a new register() call.)
        if self._current.get(agent_id) == key_epoch:
            del self._current[agent_id]
        return rec

    # -- diagnostics ----------------------------------------------------

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._current

    def __len__(self) -> int:
        return len(self._current)

    def __repr__(self) -> str:
        return (
            f"InMemoryKeyRegistry(agents={len(self._current)}, "
            f"revoked={len(self._revoked)})"
        )
