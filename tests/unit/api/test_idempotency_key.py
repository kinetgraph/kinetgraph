# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the ``Idempotency-Key`` trust boundary.

The header is folded into the deterministic
``event_id`` hash by ``_deterministic_event_id`` in
``kntgraph.api.intent_router``. Three threat models:

  1. **DoS by oversized key** — an arbitrarily large
     header value forces ``json.dumps`` to walk the
     full payload and ``uuid5`` to hash it; both are
     O(n) in the input length.
  2. **Header injection** — CRLF in the value can
     corrupt any logger that echoes the raw key.
  3. **Event-id spoofing** — a key containing CR/LF or
     a JSON separator could (in pathological cases)
     collide with another request's deterministic id
     and bypass idempotency dedup.

The sanitizer (``_sanitize_idempotency_key``) enforces
a 128-char cap and rejects control characters. Empty
or whitespace-only values collapse to ``""`` (the
historical default). The helper is module-scope and
deliberately does NOT import FastAPI; it raises
``ValueError`` on bad input and the FastAPI
endpoint converts to ``HTTPException(400)``.
"""

from __future__ import annotations

import pytest

# fastapi is an opt-in dep; skip the module when missing
# (mirrors the other api tests).
pytest.importorskip("fastapi")

from kntgraph.api.intent_router.helpers import (  # noqa: E402
    _MAX_IDEMPOTENCY_KEY_LEN,
    _sanitize_idempotency_key,
)


class TestNormalPath:
    def test_none_returns_empty(self):
        assert _sanitize_idempotency_key(None) == ""

    def test_empty_returns_empty(self):
        assert _sanitize_idempotency_key("") == ""

    def test_whitespace_only_returns_empty(self):
        # Whitespace alone is treated as "no key".
        assert _sanitize_idempotency_key("   ") == ""
        assert _sanitize_idempotency_key("\t\t") == ""

    def test_simple_key_passes_through(self):
        assert _sanitize_idempotency_key("create-order-42") == "create-order-42"

    def test_uuid_string_passes_through(self):
        k = "550e8400-e29b-41d4-a716-446655440000"
        assert _sanitize_idempotency_key(k) == k


class TestLengthCap:
    def test_max_length_accepted(self):
        k = "a" * _MAX_IDEMPOTENCY_KEY_LEN
        assert _sanitize_idempotency_key(k) == k

    def test_one_over_rejected(self):
        with pytest.raises(ValueError, match="too long"):
            _sanitize_idempotency_key("a" * (_MAX_IDEMPOTENCY_KEY_LEN + 1))

    def test_very_long_rejected(self):
        """A 1 MB key would take seconds to hash and
        waste memory; the sanitizer rejects early.
        """
        with pytest.raises(ValueError, match="too long"):
            _sanitize_idempotency_key("a" * (1024 * 1024))


class TestControlCharacters:
    @pytest.mark.parametrize(
        "bad_char, name",
        [
            ("\r", "carriage return"),
            ("\n", "line feed"),
            ("\t", "tab"),
            ("\x00", "NUL"),
            ("\x07", "BEL"),
            ("\x1b", "ESC"),
            ("\x7f", "DEL"),
        ],
    )
    def test_control_chars_rejected(self, bad_char, name):
        with pytest.raises(ValueError, match="control") as exc:
            _sanitize_idempotency_key(f"prefix{bad_char}suffix")
        assert name != ""  # parametrize hook
        assert "control" in str(exc.value).lower()

    def test_crlf_injection_attempt_rejected(self):
        """The classic log-injection vector: a key
        containing ``\\r\\n INJECTED LOG LINE\\r\\n``.
        """
        malicious = "valid-prefix\r\n2026-06-23 ERROR admin logged in from 10.0.0.1\r\n"
        with pytest.raises(ValueError, match="control"):
            _sanitize_idempotency_key(malicious)

    def test_null_byte_injection_rejected(self):
        """Null-byte truncation: ``key\\x00admin=true``
        could be interpreted as ``key`` by some
        string-handling code.
        """
        with pytest.raises(ValueError, match="control"):
            _sanitize_idempotency_key("key\x00admin=true")

    def test_unicode_separator_rejected(self):
        """Unicode line/paragraph separators are valid
        codepoints but break log parsers in the same
        way CRLF does.
        """
        for sep in ("\u2028", "\u2029", "\u200b"):
            with pytest.raises(ValueError, match="control"):
                _sanitize_idempotency_key(f"key{sep}tail")


class TestWhitespaceAround:
    def test_leading_trailing_whitespace_preserved(self):
        """We do NOT strip — the sanitiser only collapses
        all-whitespace keys to ``""``. A key with
        leading/trailing whitespace is unusual but
        not necessarily malicious (e.g. ``" key "`` is
        technically distinct from ``"key"``).
        """
        assert _sanitize_idempotency_key(" key ") == " key "

    def test_internal_space_allowed(self):
        # Spaces are not in the disallowed set; clients
        # sometimes use them in semantic keys.
        assert _sanitize_idempotency_key("my idempotency key") == "my idempotency key"


class TestTypeValidation:
    def test_non_string_raises(self):
        """Defensive: even though FastAPI gives us a
        str, the helper should never silently coerce
        non-strings.
        """
        with pytest.raises(ValueError):
            _sanitize_idempotency_key(12345)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            _sanitize_idempotency_key(["a"])  # type: ignore[arg-type]


class TestIntegrationWithIntentRouter:
    """
    End-to-end via the FastAPI TestClient: a real
    POST with a malicious Idempotency-Key is
    rejected with 400 BEFORE the auth check completes
    (we hit it as a separate concern), and a valid
    request flows through.
    """

    def _build_app_client(self):
        from fastapi.testclient import TestClient

        from kntgraph.api import create_app
        from kntgraph.api.auth import AuthError
        from kntgraph.core.result import Err, Ok
        from kntgraph.agents.tools.protocol import (
            Tool,
            ToolRegistry,
        )

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
                from kntgraph.security import (
                    Principal,
                    Role,
                )

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
            registry=registry,  # type: ignore[arg-type]
            verifier=verifier,  # type: ignore[arg-type]
        )
        return TestClient(app), log

    def test_malicious_idempotency_key_returns_400(self):
        client, log = self._build_app_client()
        r = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                # CR/LF in the key — classic log
                # injection attempt.
                "Idempotency-Key": "evil\r\nINJECTED\r\n",
            },
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        assert r.status_code == 400
        assert "control" in r.json()["detail"].lower()
        # No event was emitted.
        assert len(log.events) == 0

    def test_oversized_key_returns_400(self):
        client, log = self._build_app_client()
        r = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "x" * 1024,
            },
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        assert r.status_code == 400
        assert "too long" in r.json()["detail"].lower()
        assert len(log.events) == 0

    def test_valid_key_flows_through(self):
        client, log = self._build_app_client()
        r = client.post(
            "/agents/agent-1/intents",
            headers={
                "X-API-Key": "key-for-a1",
                "Idempotency-Key": "create-order-42",
            },
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        # 202 Accepted — the request was emitted.
        assert r.status_code == 202, r.text
        assert len(log.events) == 1

    def test_no_idempotency_key_still_works(self):
        """Sanity check: the previous behaviour (no
        header → empty string → deterministic hash)
        is preserved.
        """
        client, log = self._build_app_client()
        r = client.post(
            "/agents/agent-1/intents",
            headers={"X-API-Key": "key-for-a1"},
            json={
                "type": "tool.invoke",
                "tool": "fake.echo",
                "args": {"msg": "hi"},
            },
        )
        assert r.status_code == 202
        assert len(log.events) == 1
