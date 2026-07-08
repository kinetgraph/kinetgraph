# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
16 — Continuity: recency-aware agent.

Demonstrates the `continuity` tier of memory (ADR-014):

  - `ContinuityManager` records state-of-use events
    (tool_used, entity_seen, category_chosen) per
    (tenant, user) pair.
  - `recency_suggest(tenant, user, slot)` returns the
    last chosen value for a categorical slot — used by
    agents to pre-fill inputs with the user's most
    recent choice.
  - PII gate: `record_entity_seen` accepts ONLY a
    `sha256:...` fingerprint, never a raw value.
  - Sliding TTL: every write renews the TTL.
  - LGPD `clear`: erases the continuity state and
    disables `recency_suggest` until the next
    `tool_used`.

The example plays out a fictional day in the life of a
PME's accounting assistant:

  1. The user issues an NF-e with CFOP 6102.
  2. The agent records the tool call and the CFOP
     chosen.
  3. The user issues a second NF-e with a different
     client (CNPJ); the agent records the entity
     seen (HASH only).
  4. The user logs out.
  5. The next day, the user starts a NEW session and
     asks for "the same CFOP as yesterday". The agent
     reads `recency_suggest(..., "cfop")` and gets the
     answer without prompting the user.
  6. The user then exercises LGPD right-to-erasure via
     `clear()`. Subsequent `recency_suggest` returns
     None, even though the EventLog retains the history.

Configuration is loaded from env / `.env`. Requires Redis
running on localhost:6379. Set `FMH_REDIS_FAKE=1` to use
`fakeredis` in-process instead.

Run:
    docker run -d -p 6379:6379 --name fmh-redis redis
    python examples/16_continuity_recency.py
"""

from __future__ import annotations

import asyncio
import hashlib

from kntgraph.infra.redis._event_log import RedisEventLogAdapter
from kntgraph.infra.redis._memory import RedisContinuityStorage
from kntgraph.memory.continuity import ContinuityManager
from kntgraph.stream.event_log import EventLog

from _lib.redis_or_fake import make_redis_client


TENANT_ID = "t-acme"
USER_ID = "u-accountant"


async def day1_issue_two_invoices(
    cm: ContinuityManager,
) -> None:
    """
    The user issues two NF-e in one day. We record
    each as a ``continuity.tool_used`` event and the
    chosen CFOP as ``continuity.category_chosen``.
    """
    print("\n== Day 1 ==")
    print("  Issuing NF-e #1 (CFOP 6102)...")
    await cm.create(tenant_id=TENANT_ID, user_id=USER_ID)
    await cm.record_tool_used(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        tool="invoice.issue",
        params_fingerprint=hashlib.sha256(b"NF-e #1 payload").hexdigest()[:16],
        result_signature=hashlib.sha256(b"NF-e #1 result").hexdigest()[:16],
        latency_ms=312,
    )
    await cm.record_category_chosen(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        slot="cfop",
        value="6102",
    )

    print("  Issuing NF-e #2 (CFOP 7102, different client)...")
    # The new client's CNPJ is recorded as a HASH, not raw
    # (PII gate, ADR-014 §2.7).
    client_cnpj = "12.345.678/0001-90"
    await cm.record_tool_used(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        tool="invoice.issue",
        params_fingerprint=hashlib.sha256(b"NF-e #2 payload").hexdigest()[:16],
        result_signature=hashlib.sha256(b"NF-e #2 result").hexdigest()[:16],
        latency_ms=421,
    )
    await cm.record_entity_seen(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        kind="cnpj",
        value_hash=cm.hash_value(client_cnpj),
        source="tool_result",
    )
    await cm.record_category_chosen(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        slot="cfop",
        value="7102",
    )

    state = await cm.read(TENANT_ID, USER_ID)
    assert state is not None
    print(f"  last_tools:      {sorted(state.last_tools.keys())}")
    print(f"  last_entities:   {sorted(state.last_entities.keys())}")
    print(f"  last_categories: {state.last_categories}")


async def day2_recency_lookup(
    cm: ContinuityManager,
) -> None:
    """
    Next day. The user starts a new session and asks the
    agent to issue another NF-e "with the same CFOP as
    yesterday". The agent reads ``recency_suggest`` and
    uses the result to pre-fill the form.
    """
    print("\n== Day 2 ==")
    print("  User: 'Issue another NF-e, same CFOP as yesterday.'")
    last_cfop = await cm.recency_suggest(
        tenant_id=TENANT_ID, user_id=USER_ID, slot="cfop"
    )
    if last_cfop is None:
        print("  agent: no prior CFOP on record — asking the user.")
        return
    # The stored value is "{value}|{ts}"; we surface only
    # the value itself.
    value = last_cfop.split("|", 1)[0]
    print(f"  agent: using CFOP {value} (from yesterday).")
    print(f"  agent: pre-filled CFOP = {value}")


async def lgpd_erasure(cm: ContinuityManager) -> None:
    """
    LGPD right-to-erasure: the user invokes
    ``continuity.cleared``. After this, ``recency_suggest``
    returns ``None`` and the cache shows empty dicts.
    The EventLog retains the full history for audit.
    """
    print("\n== LGPD erasure ==")
    print("  User: 'Forget my last activity.'")
    r = await cm.clear(tenant_id=TENANT_ID, user_id=USER_ID, reason="user_request")
    assert r.is_ok()

    state = await cm.read(TENANT_ID, USER_ID)
    assert state is not None
    print(f"  is_cleared:       {state.is_cleared()}")
    print(f"  last_tools:       {state.last_tools}")
    print(f"  last_entities:    {state.last_entities}")
    print(f"  last_categories:  {state.last_categories}")

    last_cfop = await cm.recency_suggest(
        tenant_id=TENANT_ID, user_id=USER_ID, slot="cfop"
    )
    print(f"  recency_suggest:  {last_cfop}  (None expected)")
    assert last_cfop is None


async def post_clear_recording(cm: ContinuityManager) -> None:
    """
    Events arriving AFTER ``cleared`` are still recorded —
    the user may legitimately start a new continuity cycle.
    They populate the state from scratch (not replayed
    from the pre-clear history).
    """
    print("\n== Post-clear recording ==")
    print("  Issuing a fresh NF-e (CFOP 6102)...")
    await cm.record_tool_used(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        tool="invoice.issue",
        params_fingerprint=hashlib.sha256(b"NF-e #3 payload").hexdigest()[:16],
        result_signature=hashlib.sha256(b"NF-e #3 result").hexdigest()[:16],
        latency_ms=287,
    )
    await cm.record_category_chosen(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        slot="cfop",
        value="6102",
    )

    state = await cm.read(TENANT_ID, USER_ID)
    assert state is not None
    print(f"  is_cleared:       {state.is_cleared()}  (False now)")
    print(f"  last_tools:       {sorted(state.last_tools.keys())}")
    print(f"  last_categories:  {state.last_categories}")


async def pii_gate_demo(cm: ContinuityManager) -> None:
    """
    Demonstrate the PII gate: a raw value passed to
    ``record_entity_seen`` is rejected before reaching
    the EventLog. The caller MUST hash the value first
    via ``ContinuityManager.hash_value``.
    """
    print("\n== PII gate ==")
    bad = await cm.record_entity_seen(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        kind="cnpj",
        value_hash="12.345.678/0001-90",  # raw, NOT a hash
        source="tool_result",
    )
    print(f"  raw value rejected: {bad.is_err()}  (True expected)")
    assert bad.is_err()

    good_hash = cm.hash_value("12.345.678/0001-90")
    print(f"  hash_value('12.345.678/0001-90') = {good_hash}")
    good = await cm.record_entity_seen(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        kind="cnpj",
        value_hash=good_hash,
        source="tool_result",
    )
    print(f"  hashed value accepted: {good.is_ok()}  (True expected)")
    assert good.is_ok()


async def main() -> None:
    redis = make_redis_client()
    # Flush to start from a clean slate — example is idempotent
    # but easier to read from scratch.
    await redis.flushdb()

    log = EventLog(RedisEventLogAdapter(client=redis))
    cm = ContinuityManager(event_log=log, storage=RedisContinuityStorage(client=redis))

    await day1_issue_two_invoices(cm)
    await day2_recency_lookup(cm)
    await lgpd_erasure(cm)
    await post_clear_recording(cm)
    await pii_gate_demo(cm)

    print("\nAll assertions passed.")
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
