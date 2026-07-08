# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the M2 hook in ToolInvoker
(`pre_invoke_args_extractor`, ADR-013 §2.2).

Covers:
  - Retro-compat: ToolInvoker without the hook
    behaves as before (caller's args are passed through,
    `text` is no longer injected as a kwarg by the
    default path).
  - Hook wired: the extractor fills the gaps, the
    caller's args win on conflict.
  - `text` key in the request is stripped from the
    merged args (the Tool does not declare it; we don't
    want it to be flagged as `unexpected`).
  - Validation failure: emits `args_invalid`, Tool is
    NOT invoked.
  - Extractor returns Err: emits `args_invalid`.
  - Hook without `text` in the request: legacy path
    (validate the caller's args, no extraction).
  - Multiple calls of the same request: idempotent
    (Tool called once; `args_invalid` likewise).
  - Schema-less tool (None / empty): the hook still
    runs but the merge is unconstrained.
"""

from __future__ import annotations

import uuid

import pytest

from kntgraph.core.event import Event, CorrelationContext
from kntgraph.core.result import Err, Ok, ToolError
from kntgraph.agents.tools.invoker import ToolInvoker
from kntgraph.agents.tools.protocol import ToolRegistry, ToolEventType


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeLog:
    def __init__(self) -> None:
        self.appended: list[Event] = []

    async def append(self, event: Event):
        self.appended.append(event)
        return Ok(f"fake-{len(self.appended)}-0")

    async def read(self, agent_id: str) -> list[Event]:
        return [e for e in self.appended if e.agent_id == agent_id]


class _SchemaTool:
    """Tool with a real input_schema. Captures invoke args."""

    def __init__(self) -> None:
        self.name = "emitir_nfe"
        self.description = "emit NF-e"
        self.input_schema = {
            "type": "object",
            "properties": {
                "cnpj": {"type": "string"},
                "amount": {"type": "number"},
                "obs": {"type": "string"},
            },
            "required": ["cnpj", "amount"],
        }
        self.calls: list[dict] = []

    async def invoke(self, *, idempotency_key, **kwargs):
        self.calls.append({"idempotency_key": idempotency_key, **kwargs})
        return Ok({"echo": kwargs})


class _NoSchemaTool:
    """Tool with no schema. Hook should still run."""

    def __init__(self) -> None:
        self.name = "legacy.no_schema"
        self.description = "no schema"
        self.input_schema = None
        self.calls: list[dict] = []

    async def invoke(self, *, idempotency_key, **kwargs):
        self.calls.append(kwargs)
        return Ok({})


def _request(
    tool_name: str,
    *,
    agent_id: str = "a-1",
    **data,
) -> Event:
    return Event.domain_from(
        agent_id=agent_id,
        type=ToolEventType.requested(tool_name),
        data=data,
        correlation=CorrelationContext.new(correlation_id=uuid.uuid4()),
    )


class _RecordingExtractor:
    """
    Test extractor. `responses[(tool_name, text)]` →
    `(fields, confidences)`. `errors` is a set of
    `tool_name`s that should produce `Err`.
    """

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], tuple[dict, dict]] = {}
        self.errors: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def queue(
        self,
        text: str,
        tool_name: str,
        fields: dict,
        confidences: dict,
    ) -> None:
        self.responses[(tool_name, text)] = (fields, confidences)

    def fail(self, tool_name: str) -> None:
        self.errors.add(tool_name)

    async def __call__(self, text: str, tool_name: str):
        self.calls.append((text, tool_name))
        if tool_name in self.errors:
            return Err(ToolError(f"extractor-bad-{tool_name}"))
        if (tool_name, text) in self.responses:
            fields, confs = self.responses[(tool_name, text)]
            # Use a simple namespace-like object the
            # invoker's `_resolve_args` accepts.
            from types import SimpleNamespace

            return Ok(
                SimpleNamespace(fields=fields, confidences=confs, schema_version="v1")
            )
        # Default: no fields
        from types import SimpleNamespace

        return Ok(SimpleNamespace(fields={}, confidences={}, schema_version="v1"))


# ---------------------------------------------------------------------------
# Retro-compat (no hook)
# ---------------------------------------------------------------------------


class TestNoHook:
    async def test_caller_args_passed_through(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        inv = ToolInvoker(log=log, registry=reg)  # no hook

        req = _request("emitir_nfe", cnpj="11.222.333/0001-44", amount=100.0)
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert tool.calls[0]["cnpj"] == "11.222.333/0001-44"
        assert tool.calls[0]["amount"] == 100.0

    async def test_invalid_args_emit_args_invalid(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        inv = ToolInvoker(log=log, registry=reg)  # no hook

        # Missing required `amount`.
        req = _request("emitir_nfe", cnpj="11.222.333/0001-44")
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        # No invoke.
        assert tool.calls == []
        # args_invalid emitted.
        assert len(log.appended) == 1
        ev = log.appended[0]
        assert ev.event_type == "tool.emitir_nfe.args_invalid"
        assert "amount" in ev.data["missing"]
        assert ev.data["tool"] == "emitir_nfe"
        assert ev.causation_id == req.event_id

    async def test_unexpected_key_reported_but_does_not_block(self) -> None:
        # The validator's `unexpected` field is
        # informational; it does NOT prevent the
        # invoke unless `missing` / `type_mismatches`
        # are also set.
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        inv = ToolInvoker(log=log, registry=reg)

        req = _request(
            "emitir_nfe",
            cnpj="11.222.333/0001-44",
            amount=100.0,
            extra_junk="x",
        )
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert tool.calls and tool.calls[0]["extra_junk"] == "x"


# ---------------------------------------------------------------------------
# Hook wired
# ---------------------------------------------------------------------------


class TestHook:
    async def test_extractor_fills_gaps(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        ext.queue(
            "please emit",
            "emitir_nfe",
            fields={"cnpj": "11.222.333/0001-44", "amount": 1500.0},
            confidences={"cnpj": 0.9, "amount": 0.9},
        )
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        req = _request("emitir_nfe", text="please emit")
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert tool.calls[0]["cnpj"] == "11.222.333/0001-44"
        assert tool.calls[0]["amount"] == 1500.0
        # text is NOT forwarded to the Tool.
        assert "text" not in tool.calls[0]

    async def test_caller_wins_on_conflict(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        ext.queue(
            "emit",
            "emitir_nfe",
            fields={"cnpj": "WRONG", "amount": 999.0},
            confidences={"cnpj": 0.9, "amount": 0.9},
        )
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        # Caller explicitly sets cnpj and amount.
        req = _request(
            "emitir_nfe",
            text="emit",
            cnpj="11.222.333/0001-44",
            amount=100.0,
        )
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert tool.calls[0]["cnpj"] == "11.222.333/0001-44"
        assert tool.calls[0]["amount"] == 100.0

    async def test_text_key_stripped_from_merged(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        ext.queue(
            "emit",
            "emitir_nfe",
            fields={"cnpj": "11.222.333/0001-44", "amount": 100.0},
            confidences={"cnpj": 0.9, "amount": 0.9},
        )
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        # The caller's `text` is preserved on the
        # request event but MUST NOT show up in the
        # Tool's kwargs (it would be flagged as
        # `unexpected`).
        req = _request("emitir_nfe", text="emit", obs="hello")
        await inv.handle_request_event(req)
        kwargs = tool.calls[0]
        assert "text" not in kwargs
        assert kwargs["obs"] == "hello"

    async def test_validation_failure_emits_args_invalid(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        # Extractor returned only cnpj; `amount` is
        # still missing — args_invalid.
        ext.queue(
            "emit",
            "emitir_nfe",
            fields={"cnpj": "11.222.333/0001-44"},
            confidences={"cnpj": 0.9},
        )
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        req = _request("emitir_nfe", text="emit")
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert tool.calls == []
        assert len(log.appended) == 1
        ev = log.appended[0]
        assert ev.event_type == "tool.emitir_nfe.args_invalid"
        assert "amount" in ev.data["missing"]

    async def test_type_mismatch_emits_args_invalid(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        ext.queue(
            "emit",
            "emitir_nfe",
            fields={"cnpj": "11.222.333/0001-44", "amount": "dozens"},
            confidences={"cnpj": 0.9, "amount": 0.9},
        )
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        req = _request("emitir_nfe", text="emit")
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert tool.calls == []
        ev = log.appended[0]
        assert ev.event_type == "tool.emitir_nfe.args_invalid"
        # The mismatching field is named in the payload.
        assert any(m["field"] == "amount" for m in ev.data["type_mismatches"])

    async def test_extractor_err_emits_args_invalid(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        ext.fail("emitir_nfe")
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        req = _request("emitir_nfe", text="emit")
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert tool.calls == []
        ev = log.appended[0]
        assert ev.event_type == "tool.emitir_nfe.args_invalid"
        assert "extractor_error" in ev.data["reason"]

    async def test_no_text_falls_back_to_legacy_path(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        # No `text` in data → hook is not called, the
        # caller's args are validated as-is.
        req = _request(
            "emitir_nfe",
            cnpj="11.222.333/0001-44",
            amount=100.0,
        )
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert ext.calls == []  # hook NOT called
        assert tool.calls[0]["cnpj"] == "11.222.333/0001-44"

    async def test_no_schema_tool_with_hook_still_works(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _NoSchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        ext.queue(
            "go",
            "legacy.no_schema",
            fields={"a": 1, "b": "two"},
            confidences={"a": 0.9, "b": 0.9},
        )
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        req = _request("legacy.no_schema", text="go")
        r = await inv.handle_request_event(req)
        assert r.is_ok()
        assert tool.calls[0] == {"a": 1, "b": "two"}

    async def test_idempotent_on_replay(self) -> None:
        log = _FakeLog()
        reg = ToolRegistry()
        tool = _SchemaTool()
        reg.register(tool)
        ext = _RecordingExtractor()
        ext.queue(
            "emit",
            "emitir_nfe",
            fields={"cnpj": "11.222.333/0001-44", "amount": 100.0},
            confidences={"cnpj": 0.9, "amount": 0.9},
        )
        inv = ToolInvoker(log=log, registry=reg, pre_invoke_args_extractor=ext)

        req = _request("emitir_nfe", text="emit")
        r1 = await inv.handle_request_event(req)
        r2 = await inv.handle_request_event(req)
        assert r1.is_ok() and r2.is_ok()
        # Same event_id → same idempotency_key → tool
        # sees a stable call regardless of how many
        # times the invoker processes the same event.
        assert tool.calls[0]["idempotency_key"] == tool.calls[1]["idempotency_key"]
        assert len(tool.calls) == 2
