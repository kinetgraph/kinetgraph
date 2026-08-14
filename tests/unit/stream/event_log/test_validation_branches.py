# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Branch-coverage tests for ``stream/event_log/validation.py``.

The ``check_signature`` function has 4 short-circuit
branches (legacy pass-through, signature required,
no-registry pass-through, stub-key rejection) that
existing tests do not exercise end-to-end. Pinned
here so a future refactor does not regress the
"legacy events + no registry" path or the stub-key
rejection path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kntgraph.core.event import CorrelationContext, Event
from kntgraph.stream.event_log.validation import check_signature


def _signed_event() -> Event:
    """Event with a signature attached (legacy or
    signed; we just need ``event.signature is not None``
    to exercise the signed branches).
    """
    sig = MagicMock()
    sig.alg = "ed25519-v1"
    sig.key_epoch = "0"
    sig.signature = b"\x00" * 64
    return Event.create(
        event_type="user.intent",
        agent_id="a-1",
        event_class="domain",
        correlation=CorrelationContext.new(),
        signature=sig,
    )


def test_check_signature_legacy_with_no_registry_returns_none():
    """The branch ``if not require_signatures and
    key_registry is None: return None``: the legacy
    happy path (no signature required, no registry
    configured). Pinned so a future refactor does
    not regress the legacy pass-through.
    """
    event = Event.create(
        event_type="user.intent",
        agent_id="a-1",
        event_class="domain",
        correlation=CorrelationContext.new(),
    )
    assert check_signature(event, require_signatures=False, key_registry=None) is None


def test_check_signature_with_signature_but_no_registry_returns_none():
    """The branch ``if key_registry is None: return
    None``: a signed event whose agent has no
    registry to verify against passes through
    (the wire format records the signature; consumers
    with a registry will verify on read). Pinned so
    a future refactor does not reject signed events
    whose agent has no key registered.
    """
    event = _signed_event()
    # Signed + no registry + no require: pass-through.
    assert check_signature(event, require_signatures=False, key_registry=None) is None


def test_check_signature_stub_key_returns_stub_key_error():
    """The branch ``if not isinstance(pub,
    Ed25519PublicKeyWrapper): return
    "signature_invalid:stub_key"``: when the registry
    returns a stub object (no ``cryptography`` package)
    instead of a real ``Ed25519PublicKeyWrapper``, the
    event is rejected with a distinct error string so
    operators can grep for the misconfiguration.
    Pinned so a future refactor does not silently
    accept stub keys.
    """
    from kntgraph.security import KeyEpoch

    event = _signed_event()
    # The registry returns a stub (not an
    # ``Ed25519PublicKeyWrapper``).
    registry = MagicMock()
    stub_object = object()  # NOT an Ed25519PublicKeyWrapper
    registry.public_key = MagicMock(return_value=stub_object)
    result = check_signature(event, require_signatures=True, key_registry=registry)
    assert result == "signature_invalid:stub_key"


def test_check_tenant_ownership_returns_none_when_principal_owns():
    """The branch ``if principal.owns(event.agent_id):
    return None`` in ``check_tenant_ownership``: a
    non-admin principal that owns the agent_id
    (i.e. the event lives under the principal's
    tenant) passes through. Pinned so a future
    refactor does not accidentally treat owning
    principals as cross-tenant violators.
    """
    from kntgraph.core.event import CorrelationContext, Event
    from kntgraph.security import Principal, Role

    # Event under ``tenant-A``; principal owns
    # ``tenant-A``.
    event = Event.create(
        event_type="user.intent",
        agent_id="tenant-A.agent-1",
        event_class="domain",
        correlation=CorrelationContext.new(),
    )
    principal = Principal(
        agent_id="tenant-A.agent-1",
        role=Role.agent,
        tenant_id="tenant-A",
        key_id="k1",
    )
    from kntgraph.stream.event_log.validation import check_tenant_ownership

    assert check_tenant_ownership(event, principal) is None
