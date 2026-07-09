# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the ``agent_id`` trust boundary.

The validator (``kntgraph.core.event._validate_agent_id``)
runs in two places:

  1. ``Event.__post_init__`` — every time a domain
     object is constructed.
  2. ``EventLog.append`` — re-validates at the Redis
     write seam so events built via ``Event.from_dict``
     (which can construct frozen dataclasses by passing
     through the constructor without ``__post_init__``)
     cannot bypass the check.

The validator enforces a strict ASCII identifier shape
(``[A-Za-z0-9._:-]{1,128}``) because ``agent_id``
flows directly into Redis Stream keys:

    knt:agents:{agent_id}:events
    knt:eventids:{event_id}

A string containing ``:`` could collide with key
prefixes; ``*`` and whitespace break SCAN patterns
used by operators and monitoring dashboards; ``..``
and long strings defeat log readability and rate-limit
lookups. The cap at 128 chars also bounds memory in
the EventLog's in-memory cache.
"""

from __future__ import annotations


import uuid
import pytest

from kntgraph.core.event import CorrelationContext, Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(agent_id: str = "agent-1") -> Event:
    """Minimal valid event for tests that build a real
    Event. Bypasses ``__post_init__`` only via the
    constructor — never via ``from_dict`` or attribute
    mutation.
    """
    return Event.create(
        event_class="domain",
        event_type="test.event.created",
        agent_id=agent_id,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# Event.__post_init__: the standard constructor path
# ---------------------------------------------------------------------------


class TestEventConstructorRejectsBadAgentId:
    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            _make_event(agent_id="")

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            _make_event(agent_id="   ")

    def test_non_string_rejected(self):
        with pytest.raises(TypeError, match="must be str"):
            Event.create(
                event_class="domain",
                event_type="test.event.created",
                agent_id=12345,  # type: ignore[arg-type],
                correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
            )

    def test_too_long_rejected(self):
        # 129 chars: the regex requires <= 128.
        with pytest.raises(ValueError, match="128"):
            _make_event(agent_id="a" * 129)

    def test_max_length_accepted(self):
        e = _make_event(agent_id="a" * 128)
        assert e.agent_id == "a" * 128


# ---------------------------------------------------------------------------
# Character set: Redis key safety
# ---------------------------------------------------------------------------


class TestEventConstructorRejectsDangerousChars:
    @pytest.mark.parametrize(
        "agent_id",
        [
            # Characters that could break Redis SCAN
            # patterns, monitoring dashboards, or HTTP
            # header parsing.
            "agent/with/slashes",
            "agent with spaces",
            "agent\nwith\nnewlines",
            "agent\twith\ttab",
            "agent*with*glob",
            "agent?with?question",
            "agent[with]brackets",
            'agent"with"quote',
            "agent'with'apostrophe",
            "agent\\with\\backslash",
            "agent;semicolon",
            "agent<with>ltgt",
            "agent\x00null",
            # Path-traversal style identifiers.
            "../etc/passwd",
        ],
    )
    def test_redis_dangerous_chars_rejected(self, agent_id):
        with pytest.raises(ValueError, match=r"\[A-Za-z0-9"):
            _make_event(agent_id=agent_id)

    def test_unicode_rejected(self):
        """Non-ASCII chars (e.g. accented) are rejected so
        that operators can read keys byte-by-byte.
        """
        with pytest.raises(ValueError, match=r"\[A-Za-z0-9"):
            _make_event(agent_id="agente-ção")

    def test_emoji_rejected(self):
        with pytest.raises(ValueError, match=r"\[A-Za-z0-9"):
            _make_event(agent_id="agent-🤖")

    @pytest.mark.parametrize(
        "agent_id",
        [
            "agent-1",
            "agent_1",
            "agent.1",
            "agent:1",
            "AGENT-1",
            "ABC",
            "a",
            "0",
            "tenant:42:agent",
            "x.y.z:1:2:3",
            "_underscore_at_start",
            "many-dashes-and_underscores.and.dots:and:colons",
            "tenant.subdomain:42:agent-A",
        ],
    )
    def test_safe_chars_accepted(self, agent_id):
        e = _make_event(agent_id=agent_id)
        assert e.agent_id == agent_id


# ---------------------------------------------------------------------------
# Defence in depth: Event.from_dict bypass + EventLog.append
# ---------------------------------------------------------------------------


class TestEventLogAppendValidatesAgentId:
    """
    The contract under test: even when an Event is
    constructed via ``Event.from_dict`` with a malformed
    ``agent_id`` (which can construct frozen dataclasses
    without running ``__post_init__``), the
    ``EventLog.append`` boundary rejects the write and
    returns ``Err(PersistenceError)`` BEFORE any Redis
    XADD is attempted.
    """

    def test_from_dict_with_valid_agent_id(self):
        e = Event.create(
            event_class="domain",
            event_type="test.event.created",
            agent_id="agent-1",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        d = {
            "event_id": str(e.event_id),
            "agent_id": e.agent_id,
            "event_type": e.event_type,
            "event_class": e.event_class,
            "timestamp": e.timestamp.isoformat(),
            "data": dict(e.data),
            "correlation": {
                "correlation_id": str(e.correlation.correlation_id),
                "causation_id": (
                    str(e.correlation.causation_id)
                    if e.correlation.causation_id
                    else None
                ),
                "span_id": str(e.correlation.span_id),
                "metadata": dict(e.correlation.metadata),
            },
        }
        e2 = Event.from_dict(d)
        assert e2.agent_id == "agent-1"

    def test_from_dict_with_malformed_agent_id_is_accepted_at_construction(
        self,
    ):
        """Pins the threat model: ``Event.from_dict`` does
        NOT run ``__post_init__`` (frozen dataclass
        bypass). A caller that reads a corrupted Redis
        entry would produce an Event with a malformed
        agent_id. The defence is the re-validation at
        the ``EventLog.append`` boundary.
        """
        # The dictionary MUST contain a syntactically
        # valid ``event_type`` (so the from_dict parser
        # does not reject it for unrelated reasons) and
        # an ``agent_id`` containing a colon.
        e = Event.create(
            event_class="domain",
            event_type="test.event.created",
            agent_id="placeholder",  # used only to satisfy the constructor,
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        d = {
            "event_id": str(e.event_id),
            "agent_id": "agent:scanner",  # malformed
            "event_type": e.event_type,
            "event_class": e.event_class,
            "timestamp": e.timestamp.isoformat(),
            "data": dict(e.data),
            "correlation": {
                "correlation_id": str(e.correlation.correlation_id),
                "causation_id": None,
                "span_id": str(e.correlation.span_id),
                "metadata": dict(e.correlation.metadata),
            },
        }
        # Event.from_dict succeeds — but the resulting
        # Event has a malformed agent_id. The defence
        # is at the EventLog.append boundary.
        e2 = Event.from_dict(d)
        assert e2.agent_id == "agent:scanner"

    def test_append_rejects_malformed_agent_id(self):
        """
        Wire a real EventLog (via the test double) and
        verify that an Event carrying a malformed
        agent_id is rejected with PersistenceError
        before any XADD is attempted.
        """
        # Lazy imports so this module remains
        # collectable even if the optional extras are
        # missing.
        from kntgraph.stream.event_log import EventLog

        # Mock the underlying redis client so we can
        # observe whether XADD was called.
        class _MockRedis:
            def __init__(self):
                self.xadd_called = False

            async def eval(self, script, numkeys, *args, **kwargs):
                self.xadd_called = True
                return b"1-0"

        fake = _MockRedis()
        log = EventLog(fake)  # type: ignore[arg-type]
        # Build a malformed event by patching agent_id
        # after construction (bypasses __post_init__).
        e = Event.create(
            event_class="domain",
            event_type="test.event.created",
            agent_id="placeholder",
            correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
        )
        # Sneak past the constructor's __post_init__ by
        # using object.__setattr__ (frozen dataclass).
        # We use a SPACE here, which is rejected by the
        # strict regex but accepted by the (more
        # permissive) ``__post_init__`` in old code —
        # in practice the only path that bypasses
        # ``__post_init__`` is ``Event.from_dict``,
        # which can carry any string. The defence at
        # ``EventLog.append`` is the safety net.
        object.__setattr__(e, "agent_id", "agent with space")

        import asyncio

        result = asyncio.run(log.append(e))
        assert result.is_err()
        assert "agent_id" in str(result.err_value()).lower()
        # Critical: Redis was NEVER called.
        assert fake.xadd_called is False, (
            "EventLog.append wrote to Redis despite malformed agent_id"
        )
