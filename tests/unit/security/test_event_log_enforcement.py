# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for ``EventLog`` signature enforcement
(ADR-016 PR 5).

Covers:

  - Default behaviour (no enforcement): unsigned + signed
    events both accepted; legacy writes still work.
  - ``require_signatures=True``: unsigned events are
    rejected with ``Err(PersistenceError("signature_required"))``.
  - ``signature_warn_only=True`` + ``require_signatures=True``:
    unsigned events are logged at warning level but accepted.
  - Registry-backed verification: signed event with a
    revoked key is rejected.
  - Registry-backed verification: tampered event is rejected.
  - Registry-backed verification: valid signed event passes.
"""

from __future__ import annotations

from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio

from kntgraph.core.event import (
    CorrelationContext,
    Event,
)
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.security import (
    InMemoryKeyRegistry,
    generate_keypair,
    sign_event,
)
from kntgraph.stream.event_log import EventLog


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


def _make_event(
    *,
    agent_id: str = "session-42",
    event_type: str = "x",
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data={"k": "v"},
        correlation=CorrelationContext(
            correlation_id=uuid4(),
            causation_id=None,
            span_id=uuid4(),
        ),
    )


# ---------------------------------------------------------------------------
# Default behaviour (no enforcement)
# ---------------------------------------------------------------------------


class TestDefaultNoEnforcement:
    async def test_unsigned_accepted_by_default(self, fake_redis) -> None:
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        r = await log.append(_make_event())
        assert r.is_ok()

    async def test_signed_accepted_by_default(self, fake_redis) -> None:
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        priv, _ = generate_keypair()
        signed = sign_event(_make_event(), priv)
        r = await log.append(signed)
        assert r.is_ok()

    async def test_legacy_construction_still_works(self, fake_redis) -> None:
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        assert log._require_signatures is False
        assert log._key_registry is None
        assert log._signature_warn_only is False


# ---------------------------------------------------------------------------
# require_signatures=True
# ---------------------------------------------------------------------------


class TestRequireSignatures:
    async def test_unsigned_rejected_when_required(self, fake_redis) -> None:
        log = EventLog(RedisEventLogAdapter(client=fake_redis), require_signatures=True)
        r = await log.append(_make_event())
        assert r.is_err()
        assert "signature_required" in str(r.err_value())

    async def test_signed_accepted_when_required(self, fake_redis) -> None:
        log = EventLog(RedisEventLogAdapter(client=fake_redis), require_signatures=True)
        priv, _ = generate_keypair()
        signed = sign_event(_make_event(), priv)
        r = await log.append(signed)
        assert r.is_ok()

    async def test_warn_only_logs_but_accepts(self, fake_redis) -> None:
        log = EventLog(
            RedisEventLogAdapter(client=fake_redis),
            require_signatures=True,
            signature_warn_only=True,
        )
        r = await log.append(_make_event())
        # Accepted (no error).
        assert r.is_ok()

    async def test_unsigned_event_does_not_reach_redis_when_rejected(
        self,
        fake_redis,
    ) -> None:
        log = EventLog(RedisEventLogAdapter(client=fake_redis), require_signatures=True)
        e = _make_event(agent_id="a-rejected")
        r = await log.append(e)
        assert r.is_err()
        # The agent's stream must not exist (nothing was XADDed).
        events = await log.read("a-rejected")
        assert events == []


# ---------------------------------------------------------------------------
# Registry-backed verification
# ---------------------------------------------------------------------------


class TestRegistryBackedVerification:
    async def test_signed_with_unknown_key_rejected(self, fake_redis) -> None:
        # No key registered for this agent_id.
        log = EventLog(
            RedisEventLogAdapter(client=fake_redis),
            require_signatures=True,
            key_registry=InMemoryKeyRegistry(),
        )
        priv, _ = generate_keypair()
        signed = sign_event(_make_event(), priv)
        r = await log.append(signed)
        assert r.is_err()
        assert "unknown_key" in str(r.err_value())

    async def test_signed_with_revoked_key_rejected(
        self,
        fake_redis,
    ) -> None:
        priv, _ = generate_keypair()
        reg = InMemoryKeyRegistry()
        reg.register("session-42", priv)
        log = EventLog(
            RedisEventLogAdapter(client=fake_redis),
            require_signatures=True,
            key_registry=reg,
        )
        signed = sign_event(_make_event(), priv)
        # Revoke after signing.
        epoch = reg.current_epoch("session-42")
        reg.revoke("session-42", epoch, reason="test")
        r = await log.append(signed)
        assert r.is_err()
        assert "signature_invalid" in str(r.err_value())

    async def test_signed_with_valid_key_accepted(
        self,
        fake_redis,
    ) -> None:
        priv, _ = generate_keypair()
        reg = InMemoryKeyRegistry()
        reg.register("session-42", priv)
        log = EventLog(
            RedisEventLogAdapter(client=fake_redis),
            require_signatures=True,
            key_registry=reg,
        )
        signed = sign_event(_make_event(), priv)
        r = await log.append(signed)
        assert r.is_ok()

    async def test_tampered_event_rejected_after_register(
        self,
        fake_redis,
    ) -> None:
        priv, _ = generate_keypair()
        reg = InMemoryKeyRegistry()
        reg.register("session-42", priv)
        log = EventLog(
            RedisEventLogAdapter(client=fake_redis),
            require_signatures=True,
            key_registry=reg,
        )
        signed = sign_event(_make_event(), priv)
        # Tamper AFTER signing.
        from dataclasses import replace

        tampered = replace(signed, data={"k": "TAMPERED"})
        r = await log.append(tampered)
        assert r.is_err()
        assert "signature_invalid" in str(r.err_value())

    async def test_registry_without_require_signatures_allows_unsigned(
        self,
        fake_redis,
    ) -> None:
        # Useful during migration: producer-side signing is
        # opt-in but consumers verify. With a registry but
        # require_signatures=False, unsigned events pass
        # through (legacy producers).
        reg = InMemoryKeyRegistry()
        log = EventLog(
            RedisEventLogAdapter(client=fake_redis),
            require_signatures=False,
            key_registry=reg,
        )
        r = await log.append(_make_event())
        assert r.is_ok()


# ---------------------------------------------------------------------------
# signature_warn_only with registry
# ---------------------------------------------------------------------------


class TestWarnOnlyWithRegistry:
    async def test_warn_only_accepts_signature_failure(
        self,
        fake_redis,
    ) -> None:
        priv, _ = generate_keypair()
        reg = InMemoryKeyRegistry()
        reg.register("session-42", priv)
        log = EventLog(
            RedisEventLogAdapter(client=fake_redis),
            require_signatures=True,
            signature_warn_only=True,
            key_registry=reg,
        )
        # Sign, then revoke.
        signed = sign_event(_make_event(), priv)
        epoch = reg.current_epoch("session-42")
        reg.revoke("session-42", epoch, reason="test")
        # Warn-only: accepted.
        r = await log.append(signed)
        assert r.is_ok()


# ---------------------------------------------------------------------------
# Read path is unaffected by enforcement
# ---------------------------------------------------------------------------


class TestReadPathUnaffected:
    async def test_unsigned_event_can_be_read_back(
        self,
        fake_redis,
    ) -> None:
        # Write unsigned, read back: same shape.
        log_write = EventLog(RedisEventLogAdapter(client=fake_redis))
        e = _make_event(agent_id="a-1")
        await log_write.append(e)
        log_read = EventLog(
            RedisEventLogAdapter(client=fake_redis), require_signatures=True
        )
        events = await log_read.read("a-1")
        assert len(events) == 1
        assert events[0].signature is None

    async def test_signed_event_can_be_read_back(self, fake_redis) -> None:
        priv, pub = generate_keypair()
        log_write = EventLog(RedisEventLogAdapter(client=fake_redis))
        signed = sign_event(_make_event(agent_id="a-2"), priv)
        await log_write.append(signed)
        log_read = EventLog(
            RedisEventLogAdapter(client=fake_redis), require_signatures=True
        )
        events = await log_read.read("a-2")
        assert len(events) == 1
        assert events[0].signature is not None
        # Sanity: verify against the same key.
        from kntgraph.security import verify_event

        assert verify_event(events[0], pub) is True
