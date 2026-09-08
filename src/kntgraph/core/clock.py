# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
core.clock -- the framework clock.

Centralises the clock sources used across systems so the
"now" type, the canonical ``utcnow``, and the
"inject a clock or default to utcnow" fallback are
declared once. Before this module the framework had two
``utcnow`` definitions (``core/event/validators.py`` and
``infra/checkpoint.py``) and systems re-declared the
``now: Callable[[], datetime] | None = None`` signature
and the ``now or utcnow`` fallback by hand.

Clock rule
----------

| Use | Clock | Injected? | Persisted? |
|-----|-------|-----------|------------|
| Absolute instants, event timestamps, guards, saga deadlines | ``utcnow`` (wall-clock) | Yes | Yes |
| Pure duration on a hot path (resilience) | ``monotonic()`` | No | No |

Wall-clock (``utcnow``) is injectable so a replayed log
re-evaluates guards / timeouts with the same ``now`` as
the original run ("same World ⇒ same list[Event]").
Monotonic (``time.monotonic()``) is NEVER injected and
NEVER persisted: it measures a pure elapsed duration that
would be meaningless across a restart or a replay. The
``resilience/circuit_breaker`` is the precedent — its
``recovery_timeout`` is measured against monotonic and it
calls it inline (deterministic by construction, not by
injection).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from time import monotonic

Clock = Callable[[], datetime]

__all__ = ["Clock", "injectable_clock", "monotonic", "utcnow"]


def utcnow() -> datetime:
    """Timezone-aware UTC ``datetime`` — the framework's
    canonical wall-clock source. The single definition;
    ``infra.checkpoint.utcnow`` re-exports this."""
    return datetime.now(timezone.utc)


def injectable_clock(now: Clock | None) -> Clock:
    """Return ``now`` if given, else the canonical
    ``utcnow``. The one-line "inject or default" helper
    that Concordo systems call in ``__init__``:
    ``self._now = injectable_clock(now)``."""
    return now or utcnow
