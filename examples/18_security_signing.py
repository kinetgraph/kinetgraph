# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
import redis.asyncio as aioredis

from kntgraph.core.event import Event, correlation_middleware
from kntgraph.stream.event_log import EventLog
from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.security.keys import InMemoryKeyRegistry, generate_keypair
from kntgraph.security.signing import sign_event, verify_event


def _banner(msg: str) -> None:
    print("=" * 72)
    print(msg)
    print("=" * 72)


async def main() -> None:
    _banner("18 — Zero-Trust L1: Event Signing")

    # 1. Start correlation context (ADR-037 requirement)
    correlation_middleware.start(metadata={"example": "18"})

    # 2. Setup Redis and flush for a clean run
    redis = aioredis.from_url("redis://:redispassword@localhost:6379", db=15)
    await redis.flushdb()

    try:
        # ==========================================
        # PRODUCER SIDE
        # ==========================================
        _banner("Producer: Creating and signing event")

        # Build key registry and generate a keypair for our agent
        producer_registry = InMemoryKeyRegistry()
        priv, pub = generate_keypair()
        producer_registry.register("session-42", priv=priv)

        # EventLog enforcing signatures
        adapter = RedisEventLogAdapter(client=redis)
        producer_log = EventLog(adapter, require_signatures=True)

        # Create an event
        e = Event.domain_from(
            type="pedido.received",
            agent_id="session-42",
            data={"cliente_id": "cli-001", "valor_total": 100.0},
            correlation=correlation_middleware.current(),
        )

        # Sign and append
        signed_event = sign_event(e, producer_registry.private_key("session-42"))
        await producer_log.append(signed_event)

        print(f"✓ Event {signed_event.event_id} signed and appended securely.")
        print(f"  Signature alg: {signed_event.signature.alg}")
        print(f"  Public key (base64 snippet): {signed_event.signature.pk[:20]}...")

        # ==========================================
        # CONSUMER SIDE
        # ==========================================
        _banner("Consumer: Reading and verifying event")

        # Consumer gets the public key (usually from a durable registry)
        consumer_registry = InMemoryKeyRegistry()
        consumer_registry.register("session-42", priv=priv)  # Hydrating for the demo

        # EventLog resolving the public keys via registry
        consumer_log = EventLog(adapter, key_registry=consumer_registry)

        for read_event in await consumer_log.read("session-42"):
            if read_event.signature is None:
                print(f"WARNING: Unsigned event {read_event.event_id}")
                continue

            # Verify the signature
            public_key = consumer_registry.public_key(
                "session-42",
                key_epoch=read_event.signature.key_epoch,
            )
            is_valid = verify_event(read_event, public_key)

            assert is_valid, "Signature must verify!"
            print(f"✓ Verified event: {read_event.event_type} {read_event.event_id}")

    finally:
        correlation_middleware.clear()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
