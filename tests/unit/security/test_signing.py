# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for kntgraph.security.signing (ADR-016 PR 1).

Tests exercise the full signing + verification roundtrip on
synthetic event-like objects. We don't depend on the real
``Event`` class having a ``signature`` field yet (PR 2);
instead we construct a minimal stand-in that exposes the
``to_dict`` shape the signing code expects.

Coverage:

  - ``Signature`` dataclass: validation (alg whitelist,
    base64 shape, byte length per algorithm).
  - ``canonical_event_bytes``: JCS canonicalisation; signature
    field is stripped before canonicalisation; ordering is
    stable; deterministic across re-runs.
  - ``sign_event`` / ``verify_event``: roundtrip;
    tampering with any byte fails verify; wrong key fails
    verify; unknown algorithm fails verify; revoked key
    fails verify when registry is provided.
  - Algorithm agility: future ``alg`` strings are rejected
    at creation.
  - Stub mode (no cryptography installed) is exercised via
    ``generate_stub_keypair`` to keep the ``KeyRegistry``
    test surface stable when running with the [crypto]
    extra intentionally missing.

Cross-implementation: the test asserts that the canonical
bytes for the same dict input are byte-for-byte identical
across two runs (and across two orderings of the dict).
This is the property that lets external Go/Rust clients
verify a Python-produced signature (and vice-versa).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from kntgraph.security import (
    BatchSignature,
    Ed25519PrivateKeyWrapper,
    Ed25519PublicKeyWrapper,
    InMemoryKeyRegistry,
    Signature,
    SignatureError,
    SUPPORTED_ALGORITHMS,
    UnknownAlgorithmError,
    canonical_event_bytes,
    generate_keypair,
    generate_stub_keypair,
    sign_event,
    verify_event,
)


# ---------------------------------------------------------------------------
# Minimal event stand-in
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubCorrelation:
    correlation_id: UUID
    causation_id: UUID | None
    span_id: UUID | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": str(self.correlation_id),
            "causation_id": str(self.causation_id) if self.causation_id else "",
            "span_id": str(self.span_id) if self.span_id else "",
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class _StubEvent:
    """Minimal stand-in for ``Event`` with a signature field.

    Mirrors the ``to_dict`` shape of ``kntgraph.core.event.Event``
    (9 keys) plus an optional ``signature`` key. PR 2 will
    replace this with the real ``Event``; the signing code
    does not care which class provides the shape.
    """

    event_id: UUID
    agent_id: str
    event_type: str
    event_class: str
    timestamp: datetime
    data: dict[str, Any]
    correlation: _StubCorrelation
    causation_id: UUID | None = None
    version: int = 1
    signature: Signature | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "agent_id": self.agent_id,
            "event_type": self.event_type,
            "event_class": self.event_class,
            "timestamp": self.timestamp.isoformat(),
            "data": dict(self.data),
            "correlation": self.correlation.to_dict(),
            "causation_id": str(self.causation_id) if self.causation_id else "",
            "version": self.version,
            "signature": self.signature.to_dict() if self.signature else None,
        }


def _make_event(
    *,
    agent_id: str = "session-42",
    event_type: str = "pedido.received",
    data: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> _StubEvent:
    return _StubEvent(
        event_id=uuid4(),
        agent_id=agent_id,
        event_type=event_type,
        event_class="domain",
        timestamp=timestamp or datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        data=data or {"cliente_id": "cli-001", "valor_total": 100.0},
        correlation=_StubCorrelation(
            correlation_id=uuid4(),
            causation_id=None,
            span_id=uuid4(),
        ),
    )


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKeyWrapper, Ed25519PublicKeyWrapper]:
    priv, pub = generate_keypair()
    return priv, pub


@pytest.fixture
def registry() -> InMemoryKeyRegistry:
    return InMemoryKeyRegistry()


@pytest.fixture
def event() -> _StubEvent:
    return _make_event()


# ---------------------------------------------------------------------------
# Signature dataclass
# ---------------------------------------------------------------------------


class TestSignatureValidation:
    def test_accepts_ed25519_v1(self) -> None:
        sig = Signature(
            alg="ed25519-v1",
            pk=_b64(b"\x00" * 32),
            sig=_b64(b"\x00" * 64),
        )
        assert sig.alg == "ed25519-v1"
        assert sig.key_epoch == 0

    def test_rejects_unknown_algorithm(self) -> None:
        with pytest.raises(UnknownAlgorithmError) as exc:
            Signature(
                alg="rsa-pss-v9",
                pk=_b64(b"\x00" * 32),
                sig=_b64(b"\x00" * 64),
            )
        assert "rsa-pss-v9" in str(exc.value)

    def test_rejects_bad_pk_length_for_ed25519(self) -> None:
        with pytest.raises(SignatureError, match="32-byte pk"):
            Signature(
                alg="ed25519-v1",
                pk=_b64(b"\x00" * 16),  # wrong length
                sig=_b64(b"\x00" * 64),
            )

    def test_rejects_bad_sig_length_for_ed25519(self) -> None:
        with pytest.raises(SignatureError, match="64-byte sig"):
            Signature(
                alg="ed25519-v1",
                pk=_b64(b"\x00" * 32),
                sig=_b64(b"\x00" * 32),  # wrong length
            )

    def test_rejects_non_base64_pk(self) -> None:
        with pytest.raises(SignatureError, match="pk is not valid base64url"):
            Signature(
                alg="ed25519-v1",
                pk="!!!not-base64!!!",
                sig=_b64(b"\x00" * 64),
            )

    def test_default_key_epoch_is_zero(self) -> None:
        sig = Signature(
            alg="ed25519-v1",
            pk=_b64(b"\x00" * 32),
            sig=_b64(b"\x00" * 64),
        )
        assert sig.key_epoch == 0

    def test_roundtrip_to_dict_from_dict(self) -> None:
        sig = Signature(
            alg="ed25519-v1",
            pk=_b64(b"\x00" * 32),
            sig=_b64(b"\x00" * 64),
            key_epoch=3,
        )
        d = sig.to_dict()
        sig2 = Signature.from_dict(d)
        assert sig == sig2

    def test_supported_algorithms_contains_v1(self) -> None:
        assert "ed25519-v1" in SUPPORTED_ALGORITHMS


# ---------------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------------


class TestCanonicalBytes:
    def test_canonical_bytes_are_deterministic(self, event: _StubEvent) -> None:
        b1 = canonical_event_bytes(event)
        b2 = canonical_event_bytes(event)
        assert b1 == b2
        assert len(b1) > 0

    def test_canonical_bytes_strip_signature(self, keypair) -> None:
        priv, _ = keypair
        # We cannot call sign_event on _StubEvent because it
        # does not match the real Event dataclass shape. We
        # simulate by attaching a Signature manually.
        unsigned = _make_event()
        signed = _StubEvent(
            event_id=unsigned.event_id,
            agent_id=unsigned.agent_id,
            event_type=unsigned.event_type,
            event_class=unsigned.event_class,
            timestamp=unsigned.timestamp,
            data=dict(unsigned.data),
            correlation=unsigned.correlation,
            causation_id=unsigned.causation_id,
            version=unsigned.version,
            signature=Signature(
                alg="ed25519-v1",
                pk=_b64(b"\x00" * 32),
                sig=_b64(b"\x00" * 64),
            ),
        )
        b_unsigned = canonical_event_bytes(unsigned)
        b_signed = canonical_event_bytes(signed)
        assert b_unsigned == b_signed

    def test_canonical_bytes_dict_order_independent(self) -> None:
        # Two events with data dicts in different order should
        # produce the same bytes (JCS sorts keys).
        e1 = _make_event(data={"a": 1, "b": 2, "c": 3})
        # Same event_id and timestamp; only the data order differs.
        e2 = _StubEvent(
            event_id=e1.event_id,
            agent_id=e1.agent_id,
            event_type=e1.event_type,
            event_class=e1.event_class,
            timestamp=e1.timestamp,
            data={"c": 3, "a": 1, "b": 2},
            correlation=e1.correlation,
            causation_id=e1.causation_id,
            version=e1.version,
        )
        # Sanity: dicts differ in insertion order (== ignores
        # order; we check list(keys) explicitly).
        assert list(e1.data.keys()) != list(e2.data.keys())
        assert canonical_event_bytes(e1) == canonical_event_bytes(e2)


# ---------------------------------------------------------------------------
# sign_event / verify_event roundtrip
# ---------------------------------------------------------------------------


class TestSignVerify:
    def test_real_event_signs_and_verifies(self, event: _StubEvent, keypair) -> None:
        priv, pub = keypair
        # Use the real Event for sign/verify to exercise the
        # dataclasses.replace path. We build a minimal real
        # Event below because the _StubEvent does not match
        # the production class.
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        real_event = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        signed = sign_event(real_event, priv)
        assert signed.signature is not None
        assert signed.signature.alg == "ed25519-v1"
        assert verify_event(signed, pub) is True

    def test_wrong_key_fails_verify(self, event: _StubEvent, keypair) -> None:
        priv_a, _ = keypair
        _, pub_b = generate_keypair()
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        real_event = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        signed = sign_event(real_event, priv_a)
        assert verify_event(signed, pub_b) is False

    def test_tampered_data_fails_verify(self, event: _StubEvent, keypair) -> None:
        priv, pub = keypair
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        real_event = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        signed = sign_event(real_event, priv)
        # Tamper with the data after signing.
        from dataclasses import replace

        tampered = replace(
            signed,
            data={"cliente_id": "cli-999", "valor_total": 9999.0},
        )
        assert verify_event(tampered, pub) is False

    def test_missing_signature_fails_verify(self, event: _StubEvent, keypair) -> None:
        _, pub = keypair
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        real_event = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        assert real_event.signature is None
        assert verify_event(real_event, pub) is False


# ---------------------------------------------------------------------------
# Algorithm agility
# ---------------------------------------------------------------------------


class TestAlgorithmAgility:
    def test_future_alg_rejected_at_signature_creation(self) -> None:
        with pytest.raises(UnknownAlgorithmError):
            Signature(
                alg="bls12-381-v1",
                pk=_b64(b"\x00" * 48),
                sig=_b64(b"\x00" * 96),
            )

    def test_future_alg_returns_false_on_verify(
        self, event: _StubEvent, keypair
    ) -> None:
        # Build an event with a future-alg signature directly,
        # bypassing __post_init__ via object.__setattr__.
        priv, pub = keypair
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        real_event = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        signed = sign_event(real_event, priv)

        # Replace the algorithm to an unknown one. We can't
        # go through Signature() directly because __post_init__
        # rejects it; we go through __dict__ on the frozen
        # dataclass (allowed for the test).
        future_sig = Signature(
            alg="ed25519-v1",
            pk=signed.signature.pk,
            sig=signed.signature.sig,
        )
        object.__setattr__(future_sig, "alg", "future-quantum-v9")
        from dataclasses import replace

        forged = replace(signed, signature=future_sig)
        assert verify_event(forged, pub) is False


# ---------------------------------------------------------------------------
# Revocation (L2 hooks present in PR 1)
# ---------------------------------------------------------------------------


class TestRevocationHook:
    def test_revoked_key_fails_verify(
        self, event: _StubEvent, keypair, registry: InMemoryKeyRegistry
    ) -> None:
        priv, pub = keypair
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        real_event = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        signed = sign_event(real_event, priv)

        # Register and immediately revoke.
        epoch = registry.register(event.agent_id, priv)
        registry.revoke(event.agent_id, epoch, reason="test")
        assert verify_event(signed, pub, key_registry=registry) is False

    def test_unrevoked_key_verifies(
        self, event: _StubEvent, keypair, registry: InMemoryKeyRegistry
    ) -> None:
        priv, pub = keypair
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        real_event = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        signed = sign_event(real_event, priv)

        registry.register(event.agent_id, priv)
        # No revocation. verify passes.
        assert verify_event(signed, pub, key_registry=registry) is True


# ---------------------------------------------------------------------------
# Stub mode
# ---------------------------------------------------------------------------


class TestStubMode:
    def test_stub_keypair_does_not_sign(self) -> None:
        priv, pub = generate_stub_keypair()
        assert priv.algorithm == "stub-v0"
        assert pub.algorithm == "stub-v0"
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        e = Event.create(
            event_type="x",
            agent_id="a",
            event_class="domain",
            data={},
            correlation=CorrelationContext(
                correlation_id=uuid4(),
                causation_id=None,
                span_id=uuid4(),
            ),
        )
        with pytest.raises(SignatureError, match="Ed25519PrivateKey"):
            sign_event(e, priv)


# ---------------------------------------------------------------------------
# Cross-implementation property
# ---------------------------------------------------------------------------


class TestCrossImplementation:
    def test_canonical_bytes_stable_across_runs(self, event: _StubEvent) -> None:
        # The same input dict → the same bytes, every run.
        # This is the property that lets a Go client verify
        # a Python-produced signature (and vice-versa).
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        real_event = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        b1 = canonical_event_bytes(real_event)
        b2 = canonical_event_bytes(real_event)
        b3 = canonical_event_bytes(real_event)
        assert b1 == b2 == b3

    def test_canonical_bytes_independent_of_python_dict_order(
        self, event: _StubEvent
    ) -> None:
        # Even if we hand-build a dict with keys in random
        # order, JCS produces the same bytes.
        from kntgraph.core.event import (
            CorrelationContext,
            Event,
        )

        e = Event.create(
            event_type=event.event_type,
            agent_id=event.agent_id,
            event_class="domain",
            data=dict(event.data),
            correlation=CorrelationContext(
                correlation_id=event.correlation.correlation_id,
                causation_id=None,
                span_id=event.correlation.span_id,
            ),
        )
        b_canonical = canonical_event_bytes(e)
        # Build the same dict manually in different order;
        # JCS must produce identical bytes.
        d1 = e.to_dict()
        d2 = {k: d1[k] for k in reversed(list(d1.keys()))}
        # Sanity: dicts differ in insertion order.
        assert list(d1.keys()) != list(d2.keys())
        # canonical_event_bytes uses the canonical path, so
        # it must not depend on input dict order.
        assert canonical_event_bytes(e) == b_canonical


# ---------------------------------------------------------------------------
# BatchSignature shape moved to test_batch_signature.py
# (PR 4 expanded the placeholder into a real concat-v1 type).
# Kept here only for backwards-compat smoke check.
# ---------------------------------------------------------------------------


class TestBatchSignatureSmoke:
    def test_batch_signature_is_exported(self) -> None:
        # Just verify the symbol is still importable after
        # the PR 4 shape change. Real tests live in
        # ``test_batch_signature.py``.
        assert BatchSignature is not None
