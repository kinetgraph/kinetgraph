# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for EventLog wire format with ADR-016 L1 signature
(PR 3).

These tests use ``fakeredis.aioredis`` (in-process drop-in for
``redis.asyncio``) to exercise the full append/read cycle
without requiring a Redis server.

Coverage:

  - ``event_to_redis`` includes the ``signature`` field as
    JSON when present, empty string when absent.
  - ``parse_event`` decodes the signature back to a
    ``Signature`` instance; absence yields ``signature=None``.
  - Roundtrip: signed event → Redis Stream → re-read event
    with signature intact → ``verify_event`` returns True.
  - Unsigned event roundtrips with ``signature=None``.
  - Corrupted signature on the wire (invalid JSON) decodes
    to ``signature=None`` (defensive default).
  - Backwards compat: legacy entries written without a
    ``signature`` key still parse correctly.
"""

from __future__ import annotations

import json
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
    verify_event,
)
from kntgraph.stream.event_log import EventLog
from kntgraph.stream.event_log.codec import (
    event_to_redis,
    parse_event,
)


pytestmark_async = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    """In-process Redis (fakeredis). Auto-cleaned per test."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def registry() -> InMemoryKeyRegistry:
    return InMemoryKeyRegistry()


@pytest.fixture
def keypair():
    return generate_keypair()


def _make_event(
    *,
    agent_id: str = "session-42",
    event_type: str = "pedido.received",
    data: dict | None = None,
) -> Event:
    return Event.create(
        event_type=event_type,
        agent_id=agent_id,
        event_class="domain",
        data=data or {"cliente_id": "cli-001", "valor_total": 100.0},
        correlation=CorrelationContext(
            correlation_id=uuid4(),
            causation_id=None,
            span_id=uuid4(),
        ),
    )


# ---------------------------------------------------------------------------
# event_to_redis: signature field
# ---------------------------------------------------------------------------


class TestEventToRedis:
    def test_unsigned_event_has_empty_signature(self) -> None:
        e = _make_event()
        payload = event_to_redis(e)
        assert payload["signature"] == ""

    def test_signed_event_has_json_signature(self, keypair) -> None:
        priv, _ = keypair
        e = _make_event()
        signed = sign_event(e, priv)
        payload = event_to_redis(signed)
        # The signature is a JSON object string.
        assert payload["signature"] != ""
        sig_obj = json.loads(payload["signature"])
        assert sig_obj["alg"] == "ed25519-v1"
        assert "pk" in sig_obj
        assert "sig" in sig_obj

    def test_payload_keys_include_signature(self) -> None:
        e = _make_event()
        payload = event_to_redis(e)
        # The full set of wire keys: 12 (11 original + signature).
        assert "signature" in payload
        # Original keys still present.
        for k in (
            "event_id",
            "agent_id",
            "event_type",
            "event_class",
            "timestamp",
            "version",
            "data",
            "correlation_id",
            "causation_id",
            "span_id",
            "metadata",
        ):
            assert k in payload


# ---------------------------------------------------------------------------
# parse_event: signature decoding
# ---------------------------------------------------------------------------


def _to_bdata(payload: dict) -> dict:
    """Simulate Redis Stream entry: both keys and values as bytes."""
    return {
        (k.encode() if isinstance(k, str) else k): (
            v.encode() if isinstance(v, str) else v
        )
        for k, v in payload.items()
    }


class TestParseEvent:
    def test_parse_unsigned_event(self) -> None:
        e = _make_event()
        payload = event_to_redis(e)
        bdata = _to_bdata(payload)
        parsed = parse_event(b"1-0", bdata)
        assert parsed.signature is None
        # Other fields preserved.
        assert parsed.event_id == e.event_id
        assert parsed.event_type == e.event_type
        assert parsed.data == e.data

    def test_parse_signed_event(self, keypair) -> None:
        priv, _ = keypair
        e = _make_event()
        signed = sign_event(e, priv)
        payload = event_to_redis(signed)
        bdata = _to_bdata(payload)
        parsed = parse_event(b"1-0", bdata)
        assert parsed.signature is not None
        assert parsed.signature.alg == "ed25519-v1"
        assert parsed.signature.pk == signed.signature.pk
        assert parsed.signature.sig == signed.signature.sig
        assert parsed.signature.key_epoch == 0

    def test_parse_corrupted_signature_yields_none(self) -> None:
        e = _make_event()
        payload = event_to_redis(e)
        bdata = _to_bdata(payload)
        # Inject corrupted JSON in the signature slot.
        bdata[b"signature"] = b"this-is-not-json"
        parsed = parse_event(b"1-0", bdata)
        # Defensive default: signature=None.
        assert parsed.signature is None

    def test_parse_legacy_entry_without_signature_field(self) -> None:
        # Legacy entries (pre-ADR-016) lack the signature key
        # entirely. The parser must still reconstruct the event.
        e = _make_event()
        payload = event_to_redis(e)
        bdata = _to_bdata(payload)
        # Drop the signature key entirely (simulates a pre-ADR-016
        # entry written before the wire format changed).
        del bdata[b"signature"]
        parsed = parse_event(b"1-0", bdata)
        assert parsed.signature is None
        # Other fields intact.
        assert parsed.event_id == e.event_id
        assert parsed.data == e.data


# ---------------------------------------------------------------------------
# EventLog roundtrip via fakeredis
# ---------------------------------------------------------------------------


class TestEventLogRoundtrip:
    pytestmark = pytest.mark.asyncio

    async def test_unsigned_roundtrip(self, fake_redis) -> None:
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        e = _make_event(agent_id="a-1")
        r = await log.append(e)
        assert r.is_ok()
        # Read it back.
        events = await log.read("a-1")
        assert len(events) == 1
        assert events[0].signature is None
        assert events[0].event_id == e.event_id

    async def test_signed_roundtrip(self, fake_redis, keypair, registry) -> None:
        priv, pub = keypair
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        e = _make_event(agent_id="a-2")
        signed = sign_event(e, priv)
        await log.append(signed)
        # Read it back.
        events = await log.read("a-2")
        assert len(events) == 1
        assert events[0].signature is not None
        # Verify against the same public key.
        assert verify_event(events[0], pub) is True

    async def test_signed_roundtrip_via_registry(
        self, fake_redis, keypair, registry
    ) -> None:
        priv, pub = keypair
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        e = _make_event(agent_id="a-3")
        signed = sign_event(e, priv)
        await log.append(signed)
        events = await log.read("a-3")
        # Verify using the registry (the L2-style path).
        assert verify_event(events[0], pub, key_registry=registry) is True

    async def test_tampered_event_fails_verify(self, fake_redis, keypair) -> None:
        priv, pub = keypair
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        e = _make_event(
            agent_id="a-4", data={"cliente_id": "cli-001", "valor_total": 100.0}
        )
        signed = sign_event(e, priv)
        await log.append(signed)
        events = await log.read("a-4")
        # Tamper with the data after reading.
        from dataclasses import replace

        tampered = replace(events[0], data={"cliente_id": "cli-999"})
        assert verify_event(tampered, pub) is False

    async def test_batch_signed_roundtrip(self, fake_redis, keypair) -> None:
        priv, pub = keypair
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        signed_events = [
            sign_event(_make_event(agent_id="a-5", event_type=f"x.batch.{i}"), priv)
            for i in range(3)
        ]
        r = await log.append_batch(signed_events)
        assert r.is_ok()
        read_back = await log.read("a-5")
        assert len(read_back) == 3
        for evt in read_back:
            assert verify_event(evt, pub) is True

    async def test_idempotent_signed_replay(self, fake_redis, keypair) -> None:
        # Re-appending the same signed event is a no-op (idempotency
        # is keyed on event_id, not on signature).
        priv, pub = keypair
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        e = _make_event(agent_id="a-6")
        signed = sign_event(e, priv)
        r1 = await log.append(signed)
        r2 = await log.append(signed)
        assert r1.is_ok() and r2.is_ok()
        assert r1.unwrap() == r2.unwrap()
        events = await log.read("a-6")
        assert len(events) == 1
        assert verify_event(events[0], pub) is True

    async def test_mixed_signed_and_unsigned(self, fake_redis, keypair) -> None:
        # A producer might emit a mix; both should roundtrip.
        priv, pub = keypair
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        e1 = _make_event(agent_id="a-7", event_type="x")
        e2 = sign_event(_make_event(agent_id="a-7", event_type="y"), priv)
        await log.append(e1)
        await log.append(e2)
        events = await log.read("a-7")
        assert len(events) == 2
        # Order preserved by Redis Stream xrange (insertion order).
        assert events[0].signature is None
        assert events[1].signature is not None
        assert verify_event(events[1], pub) is True
        # Unsigned event returns False on verify (no key).
        assert verify_event(events[0], pub) is False


# ---------------------------------------------------------------------------
# Performance smoke (informational)
# ---------------------------------------------------------------------------


class TestSignatureOverhead:
    pytestmark = pytest.mark.asyncio

    async def test_signed_append_completes(self, fake_redis, keypair) -> None:
        # Not a benchmark; just confirms the signing overhead
        # does not break the EventLog append path.
        priv, _ = keypair
        log = EventLog(RedisEventLogAdapter(client=fake_redis))
        for i in range(10):
            e = sign_event(
                _make_event(agent_id="perf", event_type=f"x.{i}"),
                priv,
            )
            r = await log.append(e)
            assert r.is_ok()
        events = await log.read("perf")
        assert len(events) == 10
