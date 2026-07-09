# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
event.event -- The `Event` dataclass + builders.

Three builders (all `@classmethod`):

  - `Event.create`: the raw constructor. Use when the
    event type is not known at compile time.

  - `Event.operation_from`: emits a framework-owned
    OPERATIONAL event (`event_class="lifecycle"`).
    Validates the `type` argument against the closed
    set in `event.operational`.

  - `Event.domain_from`: emits an application-defined
    DOMAIN event (`event_class="domain"`). Rejects
    types in the `agent.*` namespace (those must go
    through `operation_from`).

The wire codec (`to_dict`, `from_dict`, `to_json`,
`from_json`) lives in `event.codec`. The deterministic
id generator lives in `event.id_helpers`. The
validators live in `event.validators`. The
operational enum lives in `event.operational`. The
correlation layer lives in `event.correlation`.

Why this split? `Event` is the framework's most
reused type; isolating it in its own module keeps
the import graph shallow. The wire codec and id
helpers are independently testable and rarely
change together with the dataclass fields, so they
deserve their own modules.

`Event.__post_init__` consults `constants.ALLOWED_EVENT_CLASSES`
to reject malformed wire data. `Event.create` calls
the validators explicitly for early-fail with a
clearer stack trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Mapping, Optional
from uuid import UUID

from .._typing import JsonValue
from ..agent_id import assert_valid_agent_id as _validate_agent_id
from .constants import ALLOWED_EVENT_CLASSES, EventClass
from .id_helpers import generate_deterministic_event_id
from .operational import OperationalEventType
from .validators import utcnow, validate_data, validate_event_type

if TYPE_CHECKING:
    from .correlation import CorrelationContext
    from kntgraph.security.signing import Signature

if TYPE_CHECKING:
    from .correlation import CorrelationContext


@dataclass(frozen=True, slots=True)
class Event:
    """
    Immutable fact. The only way the world changes.

    `event_class` partitions the same Redis Stream into two logical
    dimensions:

      - "lifecycle"  — operational phase transitions of the agent itself
      - "domain"     — business-level events produced/consumed by the agent

    A given event belongs to exactly one class. An event with class
    "lifecycle" controls the operational phase; an event with class
    "domain" is the business fact recorded.

    The agent's CURRENT operational phase is derived from the last
    "lifecycle" event (see `current_operational_phase`).

    The agent's CURRENT domain state is derived from the last N
    "domain" events (see projection functions in the application).

    `event_class` is validated at construction time (see
    ``__post_init__``). The Literal type hint is a STATIC
    promise; the runtime check is what protects us from
    corrupted wire data and buggy producers.
    """

    event_id: UUID
    agent_id: str
    event_type: str
    event_class: EventClass
    timestamp: datetime
    data: Mapping[str, JsonValue]
    correlation: "CorrelationContext"
    causation_id: Optional[UUID] = None
    version: int = 1
    # Optional Ed25519 signature (ADR-016 L1). ``None`` for
    # events written before this ADR or for events whose
    # producer has not enabled signing. ``Optional`` keeps
    # the field additive: existing call sites and
    # serialised-on-the-wire events continue to load and
    # roundtrip unchanged. See ``kntgraph.security`` for
    # the signing primitives.
    signature: Optional["Signature"] = None

    def __post_init__(self) -> None:
        # The Literal type is a static-only contract. The wire
        # decoder (``from_dict``, ``_parse_event``) and any
        # direct constructor call must ALSO pass the
        # runtime check, otherwise the rest of the framework
        # silently filters out events it cannot classify.
        validate_event_type(self.event_type)
        _validate_agent_id(self.agent_id)
        if self.event_class not in ALLOWED_EVENT_CLASSES:
            raise ValueError(
                f"Invalid event_class {self.event_class!r}. "
                f"Allowed values: {sorted(ALLOWED_EVENT_CLASSES)}. "
                f"This usually means a corrupted Redis entry "
                f"or a producer emitting an unknown class."
            )

    # ------------------------------------------------------------------ build

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        agent_id: str,
        event_class: EventClass,
        correlation: Optional["CorrelationContext"] = None,
        data: Optional[Mapping[str, JsonValue]] = None,
        causation_id: Optional[UUID] = None,
        event_id: Optional[UUID] = None,
        timestamp: Optional[datetime] = None,
        version: int = 1,
        signature: Optional["Signature"] = None,
    ) -> "Event":
        """
        Builds an Event. The `event_id` defaults to uuid5 of
        (agent_id, event_type, payload) so re-emitting the same
        event produces the same id (idempotency).

        If `causation_id` is provided, it ALSO enters the hash,
        so events that causally depend on different parents get
        different ids (a defensive check against collisions).

        If `causation_id` is None, the hash uses a stable
        placeholder rather than a random uuid4 — this is what
        makes `Event.create(...)` idempotent by default.

        Prefer `operation_from` or `domain_from` over `create`:
        those validate the event_type against the appropriate
        namespace and pin `event_class` automatically.

        ``signature`` (ADR-016 L1) is forwarded to the
        constructor. Callers normally use ``sign_event`` from
        ``kntgraph.security`` to attach a signature; this
        parameter is here for ``from_dict`` roundtrips.
        """
        # Early-fail on malformed fields. The same checks
        # also run in `__post_init__` (defence in depth for
        # direct `Event(...)` construction), but raising
        # here gives a clearer stack trace at the call site.
        # ADR-037: ``correlation`` is required. Check it
        # BEFORE the other validators so callers see
        # ``TypeError`` for the missing kwarg first (it is
        # the most fundamental contract violation). The
        # other validators raise ``ValueError`` for shape
        # problems with the fields that ARE provided.
        if correlation is None:
            raise TypeError(
                "Event.create requires a non-None 'correlation' "
                "(ADR-037). Pass a CorrelationContext "
                "(e.g. correlation=CorrelationContext.new(...)) "
                "or propagate from a parent event "
                "(correlation=parent.correlation)."
            )
        validate_event_type(event_type)
        _validate_agent_id(agent_id)
        validate_data(data)
        payload = dict(data) if data else {}
        ts = timestamp or utcnow()
        eid = event_id or generate_deterministic_event_id(
            causation_id=causation_id or "root",
            agent_id=agent_id,
            event_type=event_type,
            data=payload,
        )
        return cls(
            event_id=eid,
            agent_id=agent_id,
            event_type=event_type,
            event_class=event_class,
            timestamp=ts,
            data=payload,
            correlation=correlation,
            causation_id=causation_id,
            version=version,
            signature=signature,
        )

    # -------------------------------------------------- framework-owned builder

    @classmethod
    def operation_from(
        cls,
        *,
        agent_id: str,
        type: OperationalEventType,
        data: Optional[Mapping[str, JsonValue]] = None,
        correlation: Optional["CorrelationContext"] = None,
        causation_id: Optional[UUID] = None,
        event_id: Optional[UUID] = None,
        timestamp: Optional[datetime] = None,
    ) -> "Event":
        """
        Builds a framework-owned OPERATIONAL event (lifecycle class).

        The `type` argument is an `OperationalEventType` enum value.
        Only the framework's closed set of event_types is accepted
        — this guarantees a stable mapping from `event_type` to
        `OperationalPhase` and a consistent event_class.

        Application code uses this when the framework itself drives
        a phase transition (e.g. the runner promotes "spawned"
        to "idle"). Use `domain_from` for business events.
        """
        return cls.create(
            event_type=type.value,
            agent_id=agent_id,
            event_class="lifecycle",
            data=data or {},
            correlation=correlation,
            causation_id=causation_id,
            event_id=event_id,
            timestamp=timestamp,
        )

    # ----------------------------------------------- application-side builder

    @classmethod
    def domain_from(
        cls,
        *,
        agent_id: str,
        type: str,
        data: Optional[Mapping[str, JsonValue]] = None,
        correlation: Optional["CorrelationContext"] = None,
        causation_id: Optional[UUID] = None,
        event_id: Optional[UUID] = None,
        timestamp: Optional[datetime] = None,
    ) -> "Event":
        """
        Builds an APPLICATION-defined DOMAIN event.

        `type` is a free-form string chosen by the application
        (e.g. "document.received", "invoice.paid"). The framework
        does NOT enumerate domain event types — the application
        owns that vocabulary and is responsible for projecting
        domain phases from the resulting events.

        Pin `event_class` to "domain" automatically; rejects
        attempts to use a `type` that collides with the framework's
        operational namespace ("agent.*") — those must go through
        `operation_from`.
        """
        if isinstance(type, str) and type.startswith("agent."):
            raise ValueError(
                f"Domain event type {type!r} collides with the "
                f"framework's operational namespace. Use "
                f"Event.operation_from(OperationalEventType.AGENT_*) "
                f"to emit an operational event."
            )
        if not isinstance(type, str) or not type:
            raise TypeError(f"Domain event type must be a non-empty str, got {type!r}")
        return cls.create(
            event_type=type,
            agent_id=agent_id,
            event_class="domain",
            data=data or {},
            correlation=correlation,
            causation_id=causation_id,
            event_id=event_id,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------ wire

    def to_dict(self) -> dict:
        """
        Serialise to a plain dict. Delegates to
        `event.codec.event_to_dict` so the wire
        format lives in one place. Kept on the
        instance for ergonomic call sites
        (`event.to_dict()`).
        """
        # Local import to avoid the cycle
        # `event` ↔ `codec` at module load time.
        from .codec import event_to_dict

        return event_to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        """
        Inverse of ``to_dict``. Delegates to
        ``event.codec.event_from_dict`` so the
        wire format lives in one place. Kept as
        a classmethod for ergonomic call sites
        (`Event.from_dict(d)`).
        """
        from .codec import event_from_dict

        return event_from_dict(d)

    def to_json(self) -> str:
        """Serialise to JSON. Thin wrapper over
        ``codec.event_to_json``."""
        from .codec import event_to_json

        return event_to_json(self)

    @classmethod
    def from_json(cls, s: str) -> "Event":
        """Inverse of ``to_json``. Thin wrapper
        over ``codec.event_from_json``."""
        from .codec import event_from_json

        return event_from_json(s)


__all__ = ["Event"]
