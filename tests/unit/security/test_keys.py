# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for kntgraph.security.keys (ADR-016 PR 0).

The PR 0 surface is the ``KeyRegistry`` Protocol and the
``InMemoryKeyRegistry`` implementation. No actual
cryptography happens here — the keys are stdlib stubs
(sha256-derived). PR 1 swaps in real Ed25519.

These tests assert:

  1. ``generate_keypair()`` returns a ``(priv, pub)`` pair
     with 32-byte halves and a stable algorithm tag.
  2. ``InMemoryKeyRegistry`` is a valid ``KeyRegistry``
     (``isinstance`` check via ``runtime_checkable``).
  3. ``register()`` allocates a monotonic epoch per
     ``agent_id`` (0, 1, 2, ...).
  4. ``register()`` is idempotent: same private key bytes
     → same epoch.
  5. ``public_key`` / ``private_key`` / ``current_epoch``
     return the right values.
  6. ``revoke()`` records an audit entry; ``is_revoked``
     returns ``True``; ``current_epoch`` is cleared if the
     revoked epoch was current.
  7. ``revoke()`` is idempotent (re-revoke returns the same
     record).
  8. ``metadata()`` carries a stable fingerprint and an
     ISO timestamp.
  9. ``revoked_keys(agent_id)`` enumerates the revocation list.
 10. Mocks can satisfy the ``KeyRegistry`` Protocol for
     downstream tests (PR 1+).
"""

from __future__ import annotations

import re

import pytest

from kntgraph.security import (
    InMemoryKeyRegistry,
    KeyEpoch,
    KeyRegistry,
    PrivateKey,
    RevocationRecord,
    generate_keypair,
    generate_stub_keypair,
)
from kntgraph.security.keys._crypto import _StubPrivateKey, _StubPublicKey


# ---------------------------------------------------------------------------
# generate_keypair
# ---------------------------------------------------------------------------


class TestGenerateKeypair:
    def test_returns_priv_and_pub(self) -> None:
        priv, pub = generate_keypair()
        # PR 1: real Ed25519 wrappers.
        from kntgraph.security.keys import (
            Ed25519PrivateKeyWrapper,
            Ed25519PublicKeyWrapper,
        )

        assert isinstance(priv, Ed25519PrivateKeyWrapper)
        assert isinstance(pub, Ed25519PublicKeyWrapper)

    def test_key_bytes_length_is_32(self) -> None:
        priv, pub = generate_keypair()
        assert len(priv.bytes) == 32
        assert len(pub.bytes) == 32

    def test_algorithm_tag_is_ed25519_v1(self) -> None:
        priv, pub = generate_keypair()
        assert priv.algorithm == "ed25519-v1"
        assert pub.algorithm == "ed25519-v1"

    def test_two_calls_produce_different_keys(self) -> None:
        a, _ = generate_keypair()
        b, _ = generate_keypair()
        assert a.bytes != b.bytes

    def test_pub_derived_from_priv_via_public_key(self) -> None:
        # Real Ed25519: priv.public_key() returns the matching
        # public key with the same bytes.
        priv, pub = generate_keypair()
        derived = priv.public_key()
        assert derived.bytes == pub.bytes


class TestGenerateStubKeypair:
    """PR 0 stubs are still available for tests that don't
    exercise the signature path."""

    def test_stub_returns_stub_objects(self) -> None:
        priv, pub = generate_stub_keypair()
        assert priv.algorithm == "stub-v0"
        assert pub.algorithm == "stub-v0"

    def test_stub_key_bytes_length_is_32(self) -> None:
        priv, pub = generate_stub_keypair()
        assert len(priv.bytes) == 32
        assert len(pub.bytes) == 32

    def test_stub_pub_derived_from_priv_via_sha256(self) -> None:
        import hashlib

        priv, pub = generate_stub_keypair()
        assert pub.bytes == hashlib.sha256(priv.bytes).digest()


# ---------------------------------------------------------------------------
# InMemoryKeyRegistry: registration & lookup
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_first_key_returns_epoch_zero(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        epoch = reg.register("session-42", priv)
        assert epoch == KeyEpoch(0)

    def test_register_second_key_returns_epoch_one(self) -> None:
        reg = InMemoryKeyRegistry()
        priv_a, _ = generate_keypair()
        priv_b, _ = generate_keypair()
        reg.register("session-42", priv_a)
        epoch = reg.register("session-42", priv_b)
        assert epoch == KeyEpoch(1)

    def test_register_is_idempotent_on_same_key(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        first = reg.register("session-42", priv)
        second = reg.register("session-42", priv)
        assert first == second
        # No second epoch allocated.
        assert reg.current_epoch("session-42") == KeyEpoch(0)

    def test_separate_agents_have_independent_epochs(self) -> None:
        reg = InMemoryKeyRegistry()
        priv_a, _ = generate_keypair()
        priv_b, _ = generate_keypair()
        reg.register("agent-a", priv_a)
        reg.register("agent-b", priv_b)
        reg.register("agent-a", priv_b)  # different key
        assert reg.current_epoch("agent-a") == KeyEpoch(1)
        assert reg.current_epoch("agent-b") == KeyEpoch(0)

    def test_public_key_returns_registered_pub(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, pub = generate_keypair()
        epoch = reg.register("session-42", priv)
        fetched = reg.public_key("session-42", key_epoch=epoch)
        assert fetched.bytes == pub.bytes

    def test_public_key_default_epoch_is_current(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, pub = generate_keypair()
        reg.register("session-42", priv)
        assert reg.public_key("session-42").bytes == pub.bytes

    def test_private_key_returns_current(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        reg.register("session-42", priv)
        assert reg.private_key("session-42").bytes == priv.bytes


# ---------------------------------------------------------------------------
# InMemoryKeyRegistry: errors
# ---------------------------------------------------------------------------


class TestErrors:
    def test_public_key_unknown_agent_raises(self) -> None:
        reg = InMemoryKeyRegistry()
        with pytest.raises(KeyError, match="no public key"):
            reg.public_key("ghost")

    def test_private_key_unknown_agent_raises(self) -> None:
        reg = InMemoryKeyRegistry()
        with pytest.raises(KeyError, match="no current private key"):
            reg.private_key("ghost")

    def test_current_epoch_unknown_agent_raises(self) -> None:
        reg = InMemoryKeyRegistry()
        with pytest.raises(KeyError, match="no current epoch"):
            reg.current_epoch("ghost")

    def test_metadata_unknown_epoch_raises(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        reg.register("session-42", priv)
        with pytest.raises(KeyError, match="no metadata"):
            reg.metadata("session-42", key_epoch=KeyEpoch(99))

    def test_revoke_unknown_key_raises(self) -> None:
        reg = InMemoryKeyRegistry()
        with pytest.raises(KeyError, match="cannot revoke unknown key"):
            reg.revoke("ghost", key_epoch=KeyEpoch(0), reason="test")


# ---------------------------------------------------------------------------
# InMemoryKeyRegistry: revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    def _setup_two_epochs(self) -> tuple[InMemoryKeyRegistry, PrivateKey, PrivateKey]:
        reg = InMemoryKeyRegistry()
        priv_a, _ = generate_keypair()
        priv_b, _ = generate_keypair()
        reg.register("session-42", priv_a)
        reg.register("session-42", priv_b)
        return reg, priv_a, priv_b

    def test_revoke_records_audit_entry(self) -> None:
        reg, priv_a, _ = self._setup_two_epochs()
        rec = reg.revoke(
            "session-42",
            key_epoch=KeyEpoch(0),
            reason="operator_key_compromised",
            revoked_by="operator-1",
        )
        assert isinstance(rec, RevocationRecord)
        assert rec.agent_id == "session-42"
        assert rec.key_epoch == KeyEpoch(0)
        assert rec.reason == "operator_key_compromised"
        assert rec.revoked_by == "operator-1"
        # ISO-8601 timestamp.
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", rec.revoked_at)

    def test_is_revoked_returns_true_after_revoke(self) -> None:
        reg, _, _ = self._setup_two_epochs()
        reg.revoke("session-42", key_epoch=KeyEpoch(0), reason="test")
        assert reg.is_revoked("session-42", KeyEpoch(0)) is True
        assert reg.is_revoked("session-42", KeyEpoch(1)) is False

    def test_revoke_current_clears_current_epoch(self) -> None:
        reg, priv_a, priv_b = self._setup_two_epochs()
        # Current is epoch 1 (the most recent register).
        assert reg.current_epoch("session-42") == KeyEpoch(1)
        reg.revoke("session-42", key_epoch=KeyEpoch(1), reason="test")
        with pytest.raises(KeyError, match="no current epoch"):
            reg.current_epoch("session-42")
        # Epoch 0 is still in the registry. The pubkey bytes
        # match priv_a's public key.
        pub_a = priv_a.public_key()
        assert reg.public_key("session-42", KeyEpoch(0)).bytes == pub_a.bytes
        # Priv 1 is no longer reachable via private_key().
        with pytest.raises(KeyError):
            reg.private_key("session-42")

    def test_revoke_old_does_not_clear_current(self) -> None:
        reg, _, _ = self._setup_two_epochs()
        reg.revoke("session-42", key_epoch=KeyEpoch(0), reason="test")
        # Current is still epoch 1.
        assert reg.current_epoch("session-42") == KeyEpoch(1)

    def test_revoke_is_idempotent(self) -> None:
        reg, _, _ = self._setup_two_epochs()
        rec1 = reg.revoke("session-42", key_epoch=KeyEpoch(0), reason="test")
        rec2 = reg.revoke("session-42", key_epoch=KeyEpoch(0), reason="test")
        assert rec1 is rec2

    def test_revoked_keys_enumerates(self) -> None:
        reg, _, _ = self._setup_two_epochs()
        reg.revoke("session-42", KeyEpoch(0), reason="first")
        reg.revoke("session-42", KeyEpoch(1), reason="second")
        revoked = reg.revoked_keys("session-42")
        assert len(revoked) == 2
        epochs = {epoch for epoch, _ in revoked}
        assert epochs == {KeyEpoch(0), KeyEpoch(1)}

    def test_revoked_keys_filters_by_agent(self) -> None:
        reg, priv_a, _ = self._setup_two_epochs()
        reg.register("other-agent", priv_a)
        reg.revoke("session-42", KeyEpoch(0), reason="test")
        reg.revoke("other-agent", KeyEpoch(0), reason="other")
        assert len(reg.revoked_keys("session-42")) == 1
        assert len(reg.revoked_keys("other-agent")) == 1


# ---------------------------------------------------------------------------
# InMemoryKeyRegistry: metadata
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_metadata_has_fingerprint_and_algorithm(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        epoch = reg.register("session-42", priv)
        meta = reg.metadata("session-42", epoch)
        assert meta.agent_id == "session-42"
        assert meta.key_epoch == epoch
        assert meta.algorithm == "ed25519-v1"
        assert len(meta.public_key_fingerprint) == 16
        # Hex characters only.
        assert re.match(r"^[0-9a-f]{16}$", meta.public_key_fingerprint)

    def test_metadata_fingerprint_is_stable(self) -> None:
        # Same private key → same fingerprint.
        reg_a = InMemoryKeyRegistry()
        reg_b = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        reg_a.register("agent-a", priv)
        reg_b.register("agent-b", priv)
        assert (
            reg_a.metadata("agent-a", KeyEpoch(0)).public_key_fingerprint
            == reg_b.metadata("agent-b", KeyEpoch(0)).public_key_fingerprint
        )

    def test_metadata_timestamp_is_iso(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        epoch = reg.register("session-42", priv)
        meta = reg.metadata("session-42", epoch)
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", meta.created_at)


# ---------------------------------------------------------------------------
# InMemoryKeyRegistry: dunders
# ---------------------------------------------------------------------------


class TestDunders:
    def test_contains(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        assert "session-42" not in reg
        reg.register("session-42", priv)
        assert "session-42" in reg

    def test_len_counts_distinct_agents(self) -> None:
        reg = InMemoryKeyRegistry()
        priv, _ = generate_keypair()
        assert len(reg) == 0
        reg.register("a", priv)
        reg.register("b", priv)
        reg.register("a", generate_keypair()[0])  # second key, same agent
        assert len(reg) == 2

    def test_repr_is_informative(self) -> None:
        reg = InMemoryKeyRegistry()
        priv_a, _ = generate_keypair()
        priv_b, _ = generate_keypair()
        reg.register("a", priv_a)
        reg.register("b", priv_b)
        # Revoking "a"'s current epoch removes it from
        # ``_current`` (the live-agent view). "b" remains.
        reg.revoke("a", KeyEpoch(0), reason="test")
        r = repr(reg)
        assert "agents=1" in r
        assert "revoked=1" in r

    def test_repr_after_revoke_old_epoch(self) -> None:
        # If the revoked epoch is NOT the current one, both
        # agents remain in the live view.
        reg = InMemoryKeyRegistry()
        priv_a1, _ = generate_keypair()
        priv_a2, _ = generate_keypair()
        priv_b, _ = generate_keypair()
        reg.register("a", priv_a1)
        reg.register("a", priv_a2)  # current = epoch 1
        reg.register("b", priv_b)
        reg.revoke("a", KeyEpoch(0), reason="old key")
        r = repr(reg)
        assert "agents=2" in r
        assert "revoked=1" in r


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class _MockKeyRegistry:
    """Minimal mock that satisfies the ``KeyRegistry`` Protocol.

    Used by downstream tests (PR 1+) to assert that any
    conforming implementation can be passed to ``EventLog``
    and other call sites.
    """

    def public_key(self, agent_id, key_epoch=0):
        return _StubPublicKey(bytes=b"\x00" * 32, algorithm="mock-v0")

    def private_key(self, agent_id):
        return _StubPrivateKey(bytes=b"\x00" * 32, algorithm="mock-v0")

    def register(self, agent_id, priv):
        return KeyEpoch(0)

    def revoke(self, agent_id, key_epoch, reason):
        from datetime import datetime, timezone

        return RevocationRecord(
            agent_id=agent_id,
            key_epoch=key_epoch,
            reason=reason,
            revoked_at=datetime.now(timezone.utc).isoformat(),
            revoked_by="mock",
        )

    def is_revoked(self, agent_id, key_epoch):
        return False

    def current_epoch(self, agent_id):
        return KeyEpoch(0)

    def metadata(self, agent_id, key_epoch):
        from kntgraph.security import KeyMetadata

        return KeyMetadata(
            agent_id=agent_id,
            key_epoch=key_epoch,
            created_at="2026-06-22T00:00:00+00:00",
            algorithm="mock-v0",
            public_key_fingerprint="0" * 16,
        )


class TestProtocolConformance:
    def test_in_memory_registry_satisfies_protocol(self) -> None:
        reg = InMemoryKeyRegistry()
        assert isinstance(reg, KeyRegistry)

    def test_mock_satisfies_protocol(self) -> None:
        mock = _MockKeyRegistry()
        assert isinstance(mock, KeyRegistry)

    def test_object_missing_methods_does_not_satisfy(self) -> None:
        class Incomplete:
            pass

        assert not isinstance(Incomplete(), KeyRegistry)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------
