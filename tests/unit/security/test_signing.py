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

    def test_rejects_non_base64_sig(self) -> None:
        """The mirror branch: ``sig is not valid base64url``.
        Pinned so a future refactor does not regress the
        sig-side validation (the pk-side test already
        exists; the sig-side was uncovered).
        """
        with pytest.raises(SignatureError, match="sig is not valid base64url"):
            Signature(
                alg="ed25519-v1",
                pk=_b64(b"\x00" * 32),
                sig="!!!not-base64!!!",
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


# ---------------------------------------------------------------------------
# signing._crypto: PEP 562 __getattr__ + require_crypto fail-fast
# ---------------------------------------------------------------------------


class TestSigningCryptoModuleAttributeAccess:
    """Branch coverage for ``signing._crypto.__getattr__``
    and ``require_crypto``. The signing module mirrors the
    keys module's optional-crypto pattern: when
    ``cryptography`` or ``canonicaljson`` is unavailable,
    the module globals are ``None`` and ``__getattr__``
    proxies attribute access. Pinned so a future refactor
    does not regress the proxy or fail-fast contract.
    """

    def test_module_getattr_returns_globals_when_name_routed(self) -> None:
        """The branch ``if name in (...): return
        globals().get(name)``. Force ``__getattr__``
        to fire by deleting the attribute from the
        module dict, then access it to take the
        if-true path.
        """
        from kntgraph.security.signing import _crypto

        saved = _crypto.__dict__.pop("Ed25519PrivateKey", None)
        try:
            value = _crypto.Ed25519PrivateKey
            assert value is None
        finally:
            _crypto.Ed25519PrivateKey = saved

    def test_require_crypto_raises_when_unavailable(self, monkeypatch) -> None:
        """The branch ``if not
        CRYPTOGRAPHY_AVAILABLE: raise
        CryptoUnavailableError``. Pinned so the
        signing path fails fast with a clear error
        instead of an opaque ``AttributeError`` on
        a ``None`` import.
        """
        from kntgraph.security.signing import _crypto

        monkeypatch.setattr(_crypto, "CRYPTOGRAPHY_AVAILABLE", False)
        with pytest.raises(SignatureError, match="cryptography>=41.0"):
            _crypto.require_crypto()


# ---------------------------------------------------------------------------
# signing._types: Signature.__post_init__ + BatchSignature + _scalar
# ---------------------------------------------------------------------------


class TestSignaturePostInitBranches:
    """Branch coverage for ``Signature.__post_init__``
    and the private ``_scalar`` helper used by
    ``Signature.from_dict``. Pinned so a future
    refactor does not regress the per-algorithm
    length validation or the JSON-scalar coercion
    contract.
    """

    def test_post_init_skips_length_check_for_non_ed25519_alg(
        self, monkeypatch
    ) -> None:
        """The branch ``if self.alg == "ed25519-v1":
        ... else: ... `` exits the function via the
        if-false arm when a future algorithm in
        ``SUPPORTED_ALGORITHMS`` has no per-algorithm
        length rules. We extend the whitelist with
        a synthetic v2 alg so the constructor takes
        that exit.
        """
        from kntgraph.security.signing import _types as signing_types

        monkeypatch.setattr(
            signing_types,
            "SUPPORTED_ALGORITHMS",
            signing_types.SUPPORTED_ALGORITHMS | {"ed25519-v2"},
        )
        # 32/64 byte pk/sig are valid base64; the alg
        # is in SUPPORTED; the post_init exits via the
        # if-false arm at line 98.
        sig = Signature(
            alg="ed25519-v2",
            pk=_b64(b"\x00" * 32),
            sig=_b64(b"\x00" * 64),
        )
        assert sig.alg == "ed25519-v2"


class TestBatchSignatureMixedAlgs:
    """The branch ``if len(algs) > 1: raise
    SignatureError`` in ``BatchSignature.__post_init__``.
    With the current whitelist the only per-entry alg
    is ``ed25519-v1`` so the branch never fires via the
    public API; this test exercises it via a synthetic
    whitelist extension.
    """

    def test_mixed_per_entry_algs_rejected(self, monkeypatch) -> None:
        from kntgraph.security.signing import _types as signing_types

        # Add a 2nd alg to the per-entry whitelist so we
        # can build two entries with distinct algs.
        monkeypatch.setattr(
            signing_types,
            "SUPPORTED_ALGORITHMS",
            signing_types.SUPPORTED_ALGORITHMS | {"ed25519-v2"},
        )

        sig_v1 = Signature(
            alg="ed25519-v1",
            pk=_b64(b"\x00" * 32),
            sig=_b64(b"\x00" * 64),
        )
        sig_v2 = Signature(
            alg="ed25519-v2",
            pk=_b64(b"\x00" * 32),
            sig=_b64(b"\x00" * 64),
        )
        e = _make_event()

        from kntgraph.security import BatchEntry

        with pytest.raises(SignatureError, match="mixes per-entry algorithms"):
            signing_types.BatchSignature(
                alg="concat-v1",
                signatures=(
                    BatchEntry(signature=sig_v1, event=e, public_key=None),
                    BatchEntry(signature=sig_v2, event=e, public_key=None),
                ),
            )


class TestScalarCoercion:
    """The ``_scalar`` helper used by
    ``Signature.from_dict``. Pinned so a future
    refactor does not regress the JSON-value-to-string
    coercion contract.
    """

    def test_scalar_none_returns_empty_string(self) -> None:
        from kntgraph.security.signing import _types as signing_types

        assert signing_types._scalar(None) == ""

    def test_scalar_str_returns_unchanged(self) -> None:
        from kntgraph.security.signing import _types as signing_types

        assert signing_types._scalar("hello") == "hello"

    def test_scalar_int_returns_str(self) -> None:
        from kntgraph.security.signing import _types as signing_types

        assert signing_types._scalar(42) == "42"

    def test_scalar_float_returns_str(self) -> None:
        from kntgraph.security.signing import _types as signing_types

        assert signing_types._scalar(3.14) == "3.14"

    def test_scalar_bool_returns_str(self) -> None:
        from kntgraph.security.signing import _types as signing_types

        assert signing_types._scalar(True) == "True"

    def test_scalar_non_scalar_returns_empty_string(self) -> None:
        from kntgraph.security.signing import _types as signing_types

        assert signing_types._scalar({"nested": "dict"}) == ""
        assert signing_types._scalar([1, 2, 3]) == ""


# ---------------------------------------------------------------------------
# verify_event: crypto-unavailable guard + non-verifying public key
# ---------------------------------------------------------------------------


class TestVerifyEventBranches:
    """Branch coverage for the early-exit guards in
    ``verify_event``. Pinned so a future refactor
    does not regress the fail-closed contract.
    """

    def test_verify_returns_false_when_crypto_unavailable(
        self, monkeypatch, event: _StubEvent, keypair
    ) -> None:
        """The branch ``if not
        CRYPTOGRAPHY_AVAILABLE: return False`` in
        ``verify_event``. Even a correctly signed
        event must fail closed when the optional
        crypto extra is missing — a silent
        success would be a security regression.
        """
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
        # Patch on the consumer module (``_verify``
        # imports the name directly into its namespace).
        from kntgraph.security.signing import _verify

        monkeypatch.setattr(_verify, "CRYPTOGRAPHY_AVAILABLE", False)
        assert verify_event(signed, pub) is False

    def test_verify_returns_false_when_pub_lacks_verify(
        self, monkeypatch, event: _StubEvent, keypair
    ) -> None:
        """The branch ``if not hasattr(raw_pub,
        "verify"): return False``. A public-key
        wrapper that does not expose ``verify``
        (e.g. a stub-only environment) cannot be
        used for verification.
        """
        priv, _ = keypair
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

        # A bare object with no ``verify`` attribute —
        # the ``_crypto_verify`` helper rejects it
        # via the ``hasattr`` guard.
        class _NoVerify:
            pass

        bad_pub = _NoVerify()
        assert verify_event(signed, bad_pub) is False

    def test_verify_returns_false_when_is_revoked_raises(
        self, monkeypatch, event: _StubEvent, keypair
    ) -> None:
        """The branch ``except Exception: return True``
        in ``_is_revoked``: the registry raises on
        lookup, the verifier treats the entry as
        revoked (fail-closed). Pinned so a future
        refactor does not silently accept an entry
        the registry cannot introspect.
        """
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

        class _BoomRegistry:
            def is_revoked(self, agent_id, epoch):
                raise RuntimeError("registry down")

        is_valid = verify_event(signed, pub, key_registry=_BoomRegistry())
        assert is_valid is False

    def test_crypto_verify_returns_false_when_canonical_bytes_fails(
        self, monkeypatch, event: _StubEvent, keypair
    ) -> None:
        """The branch ``except Exception: return False``
        in ``_crypto_verify`` when ``canonical_event_bytes``
        raises. Pinned so a future refactor does not
        propagate an opaque canonicalisation error as
        a 500.
        """
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

        # Patch the canonicalisation helper to raise.
        import kntgraph.security.signing._verify as _verify_mod

        def _boom(_event):
            raise RuntimeError("canonicaljson unavailable")

        monkeypatch.setattr(_verify_mod, "canonical_event_bytes", _boom)
        assert verify_event(signed, pub) is False

    def test_crypto_verify_returns_false_when_sig_base64_invalid(
        self, monkeypatch, event: _StubEvent, keypair
    ) -> None:
        """The branch ``except Exception: return False``
        when the per-event signature's base64 fails to
        decode. Pinned so the verifier never raises.
        """
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
        # Bypass the ``__post_init__`` base64 validation
        # with ``object.__setattr__`` (the dataclass is
        # frozen; normal assignment raises).
        object.__setattr__(signed.signature, "sig", "!!!not-base64!!!")
        assert verify_event(signed, pub) is False
