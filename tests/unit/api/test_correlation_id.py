# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the correlation_id derivation at the HTTP
intent-router boundary (ADR-065 §2.3, ADR-037 §2).

The bug fixed in this iteration: the gateway used to mint
``correlation_id = uuid4()`` on every request, so two retries
of the same request produced two different correlation_ids
even though the ``event_id`` was stable (the deterministic
UUID5 hash). The audit trail could not stitch the retry back
to the original intent.

The fix: ``correlation_id = UUID(event_id)``. The
``event_id`` is the UUID5 hash of
``(agent_id, type, target, args, idempotency_key)`` — so a
retry of the same request produces the same
``correlation_id``, and the entry event is self-identifying
as the root of its own flow.

Tests cover:

  - Same request twice → same correlation_id (the retry
  case; the pre-fix bug).
  - ``correlation_id == event_id`` for the emitted event
  (the entry-event contract).
  - Different idempotency keys produce different
  correlation_ids (the deduplication does not collapse
  distinct flows).
  - Different ``args`` produce different correlation_ids
  (the hash inputs include args, by construction).
"""

from __future__ import annotations

import pytest


pytest.importorskip("fastapi")


class TestCorrelationIdDerivation:
    """
    ADR-065 §2.3: ``correlation_id`` is derived from
    ``event_id`` so a retry of the same request
    produces the same correlation_id and the audit
    trail stitches the retry back to the original
    intent.
    """

    def _build_app_client(self):
        from fastapi.testclient import TestClient

        from kntgraph.api import create_app
        from kntgraph.api.auth import AuthError
        from kntgraph.core.result import Err, Ok
        from kntgraph.agents.tools.protocol import (
            Tool,
        )
        from kntgraph.security import (
            Principal,
            Role,
        )
        from kntgraph.tools.registry import ToolRegistry

        from ._fake_log import FakeEventLog

        class _FakeTool(Tool):
            name = "fake.echo"
            description = "Echoes the input."
            input_schema: dict = {
                "type": "object",
                "properties": {"msg": {"type": "string"}},
            }

            async def invoke(self, *, idempotency_key: str, **kwargs):
                raise NotImplementedError

        class _FakeVerifier:
            def __init__(self, bindings):
                self._bindings = bindings
                self._principals = {
                    k: Principal(
                        agent_id=v,
                        role=Role.agent,
                        tenant_id=v.partition(".")[0] or v,
                        key_id="test",
                    )
                    for k, v in bindings.items()
                }

            async def verify(self, api_key):
                if not api_key:
                    return Err(AuthError("missing", "X-API-Key required"))
                if api_key not in self._bindings:
                    return Err(AuthError("forbidden", "key not recognised"))
                return Ok(self._principals[api_key])

        registry = ToolRegistry()
        registry.register(_FakeTool())
        log = FakeEventLog()
        verifier = _FakeVerifier({"key-for-a1": "agent-1"})
        app = create_app(
            log=log,  # type: ignore[arg-type]
            registry=registry,
            verifier=verifier,
        )
        return TestClient(app), log

    def test_retry_of_same_request_produces_same_correlation_id(self):
        """
        Two POSTs with identical headers + body must
        share the same correlation_id (the
        ``correlation_id`` derives from the
        ``event_id`` hash; the hash inputs are
        identical). The pre-fix bug minted a fresh
        ``uuid4()`` per request.
        """
        client, log = self._build_app_client()
        headers = {
            "X-API-Key": "key-for-a1",
            "Idempotency-Key": "retry-flow-1",
        }
        body = {
            "type": "tool.invoke",
            "tool": "fake.echo",
            "args": {"msg": "hi"},
        }
        r1 = client.post("/agents/agent-1/intents", headers=headers, json=body)
        r2 = client.post("/agents/agent-1/intents", headers=headers, json=body)
        assert r1.status_code == 202
        assert r2.status_code == 202
        # Both requests share the same correlation_id
        # (because the hash inputs are identical).
        corr1 = log.events[0].correlation.correlation_id
        corr2 = log.events[1].correlation.correlation_id
        assert corr1 == corr2
        assert corr1 is not None

    def test_correlation_id_matches_event_id(self):
        """
        The entry event's ``correlation_id`` is the
        ``event_id`` (ADR-037 §2: the entry event is
        self-identifying as the root of its own
        flow). This makes ``correlation_id == X``
        queries on the EventLog return the request
        itself as the first event.
        """
        client, log = self._build_app_client()
        r = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "self-id-1",
            },
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        assert r.status_code == 202
        ev = log.events[0]
        assert str(ev.correlation.correlation_id) == str(ev.event_id)

    def test_different_idempotency_keys_produce_different_correlation_ids(self):
        """
        Two requests with distinct idempotency keys
        must NOT share a correlation_id — they are
        distinct flows, and the audit trail must
        keep them separate. The hash inputs include
        the idempotency key (per
        ``_deterministic_event_id``); this test
        guards against a regression where the
        correlation derivation accidentally drops
        the idempotency key.
        """
        client, log = self._build_app_client()
        body = {
            "type": "tool.invoke",
            "tool": "fake.echo",
            "args": {"msg": "hi"},
        }
        r1 = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "flow-1",
            },
            json=body,
        )
        r2 = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "flow-2",
            },
            json=body,
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        corr1 = log.events[0].correlation.correlation_id
        corr2 = log.events[1].correlation.correlation_id
        assert corr1 != corr2

    def test_different_args_produce_different_correlation_ids(self):
        """
        Two requests with the same idempotency key
        but distinct ``args`` must NOT share a
        correlation_id. The hash inputs include the
        full ``args`` payload (per
        ``_deterministic_event_id``); this guards
        against a regression where the correlation
        derivation accidentally drops the args.
        """
        client, log = self._build_app_client()
        headers = {
            "X-API-Key": "key-for-a1",
            "Idempotency-Key": "shared-key",
        }
        r1 = client.post(
            "/agents/agent-1/intents",
            headers=headers,
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "first"},
            },
        )
        r2 = client.post(
            "/agents/agent-1/intents",
            headers=headers,
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "second"},
            },
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        corr1 = log.events[0].correlation.correlation_id
        corr2 = log.events[1].correlation.correlation_id
        assert corr1 != corr2

    def test_no_idempotency_key_still_produces_deterministic_correlation_id(self):
        """
        When the request carries no
        ``Idempotency-Key``, the empty string is
        folded into the hash (per
        ``_deterministic_event_id``). Two such
        requests share a correlation_id (the empty
        key is the historical default).
        """
        client, log = self._build_app_client()
        body = {
            "type": "tool.invoke",
            "tool": "fake.echo",
            "args": {"msg": "hi"},
        }
        r1 = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json=body,
        )
        r2 = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json=body,
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        corr1 = log.events[0].correlation.correlation_id
        corr2 = log.events[1].correlation.correlation_id
        assert corr1 == corr2