# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event_log.validation -- Pre-flight checks for `EventLog.append`.

The EventLog runs three checks before touching Redis:

  - `validate_agent_id_for_redis(agent_id)`: defence in
    depth — the `agent_id` flows directly into a Redis
    Stream key, so a malformed value (containing `:`,
    `*`, or whitespace) could collide with other
    namespaces. `Event.__post_init__` already enforces
    the shape; this re-checks at the Redis boundary to
    close any path that constructs an `Event` via
    `Event.from_dict` (a frozen dataclass bypass).

  - `check_signature(event, key_registry, ...)`: ADR-016
    L1 pre-flight signature check. Returns `None` on
    pass-through or a short error string suitable for
    the `PersistenceError` message and the structured
    log.

  - `check_tenant_ownership(event, principal)`: ADR-017
    §3.3 tenant check. Returns `None` when the principal
    is allowed (no principal bound, or admin, or owns
    the tenant), or a `PersistenceError` ready to wrap
    in `Err(...)`.

All helpers are stateless and pure (no Redis access,
no logger writes) — the caller is responsible for the
structured log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import structlog

from ...core._typing import ValidatorInput
from ...core.agent_id import validate_agent_id
from ...core.event import Event
from ...core.result import PersistenceError
from ...security.keys import Ed25519PublicKeyWrapper
from ...security.keys._types import KeyEpoch
from ...security.signing import verify_event

if TYPE_CHECKING:
    from ...security import KeyRegistry, Principal


logger = structlog.get_logger()


def validate_agent_id_for_redis(agent_id: ValidatorInput) -> Optional[str]:
    """
    Thin wrapper around
    :func:`kntgraph.core.agent_id.validate_agent_id`.

    The parameter is typed as ``ValidatorInput`` (the
    recursive JSON-scalar Union) because the validator
    accepts any value and decides; the caller does not
    need to know the input type in advance. The contract
    is: the validator returns ``None`` for valid input, a
    short error string for invalid input.
    """
    return validate_agent_id(agent_id)


def check_signature(
    event: Event,
    *,
    key_registry: Optional["KeyRegistry"] = None,
    require_signatures: bool = False,
) -> Optional[str]:
    """Pre-flight signature check (ADR-016 PR 5).

    Returns ``None`` when the event passes (or when
    enforcement is off); otherwise returns a short
    error string suitable for the ``PersistenceError``
    message and the structured log.

    Behaviour:
      1. If neither ``require_signatures`` nor a
         ``key_registry`` is set: pass-through.
      2. If ``require_signatures=True`` and the event
         has ``signature=None``: return
         ``"signature_required"``.
      3. If a registry is configured and the event has
         a signature: verify via ``verify_event``.
         Failures return
         ``"signature_invalid"``.
      4. If a registry is configured but the event has
         no signature AND ``require_signatures=False``:
         skip verification (legacy event). If
         ``require_signatures=True``: caught by step 2.
    """
    if not require_signatures and key_registry is None:
        return None
    sig = getattr(event, "signature", None)
    if sig is None:
        if require_signatures:
            return "signature_required"
        return None
    if key_registry is None:
        # We have a signature but no registry to verify
        # against. Pass-through (the wire format records
        # it; consumers with a registry will verify on
        # read).
        return None
    # Look up the public key for this agent/epoch.
    try:
        pub = key_registry.public_key(event.agent_id, KeyEpoch(sig.key_epoch))
    except KeyError:
        return "signature_invalid:unknown_key"
    # The registry may return either a real
    # Ed25519PublicKeyWrapper or a stub (when the
    # ``cryptography`` package is unavailable).
    # Only real Ed25519 keys can verify signatures;
    # a stub can never be trusted, so we reject it
    # explicitly with a distinct error string so
    # operators can grep for the misconfiguration.
    if not isinstance(pub, Ed25519PublicKeyWrapper):
        return "signature_invalid:stub_key"
    if not verify_event(event, pub, key_registry=key_registry):
        return "signature_invalid"
    return None


def check_tenant_ownership(
    event: Event,
    principal: Optional["Principal"],
) -> Optional[PersistenceError]:
    """
    ADR-017 §3.3: tenant ownership check. If a
    principal is bound for this task, the event's
    agent_id must live under the principal's
    tenant (admins are exempt). This closes the
    cross-tenant attack at the EventLog boundary:
    even if a producer somehow forged an event
    with another tenant's agent_id, the write is
    refused.

    Returns ``None`` when the principal is allowed
    (no principal bound, or admin, or owns the
    tenant), or a ``PersistenceError`` ready to
    return via ``Err(...)``.
    """
    if principal is None or principal.is_admin():
        return None
    if principal.owns(event.agent_id):
        return None
    return PersistenceError(
        f"tenant_violation: principal "
        f"tenant {principal.tenant_id!r} "
        f"cannot append event with "
        f"agent_id={event.agent_id!r}"
    )


__all__ = [
    "check_signature",
    "check_tenant_ownership",
    "validate_agent_id_for_redis",
]
