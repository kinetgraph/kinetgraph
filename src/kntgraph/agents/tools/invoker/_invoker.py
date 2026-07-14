# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``ToolInvoker`` — the orchestrator class.

Module-level helpers (event emission, signal types) live in
``_emit.py`` and ``_types.py``. Public re-exports live in
the package ``__init__.py``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Optional

import structlog

from kntgraph.core.event import (
    Event,
)
from kntgraph.core.result import Result, ToolError
from kntgraph.security import principal_ctx
from kntgraph.stream.event_log import EventLog
from kntgraph.agents.tools.arg_validation import SchemaValidationError, validate_args
from ._emit import (
    emit_args_invalid,
    emit_completion,
    emit_failure,
    ms_since,
)
from ._types import ArgsInvalid
from kntgraph.agents.tools.protocol import (
    Tool,
    ToolArgValue,
    ToolRegistry,
)

if TYPE_CHECKING:
    from ...knowledge.extraction.base import ArgExtraction


# Framework-level type variable for values crossing
# the tool boundary is defined in ``_emit.py`` (no
# cycle: ``_emit.py`` does not import ``_invoker``).

logger = structlog.get_logger()

# ADR-013 §2.2: the M2 hook. The callable takes the
# user's text + the tool name and returns either an
# ``ArgExtraction`` (Ok) or a ``ToolError`` (Err). The
# ToolInvoker handles both shapes and falls back
# gracefully when the hook is not configured.
#
# The return type uses a string forward-reference
# because ``ArgExtraction`` lives in
# ``knowledge.extraction.base`` — importing it here
# would create a cycle (``tools.invoker`` is already
# depended on by ``knowledge.extraction.argument_extractor``).
PreInvokeArgsExtractor = Callable[
    [str, str],
    Awaitable["Result[ArgExtraction, ToolError]"],
]


class ToolInvoker:
    """
    Adapter-side helper: reads `.requested` events from the
    EventLog, calls the corresponding tool, and writes
    `.completed` / `.failed` back.

    Production: wrap each call in circuit breaker + retry.
    Tests: call `await invoker.run_once(agent_id)` directly.

    .. deprecated::
        ``ToolInvoker`` is on the legacy tool path. New
        code should use ``@tool_worker`` (ADR-036)
        orchestrated by ``WorkerManager``. The
        ``ToolInvoker`` is kept for tools that have
        not been migrated yet (e.g. ``PiiRedactionTool``).
        Removal target: v1.0.0. See ADR-043.

    ADR-013 M2 hook
    ---------------

    The `pre_invoke_args_extractor` parameter is the
    extension point for semantic argument extraction.
    When set, the invoker runs it before `tool.invoke`
    and merges the result with the caller's `args`
    (caller wins; the extractor fills the gaps). The
    merged dict is then validated against the Tool's
    `input_schema`. On validation failure the request
    is short-circuited with a `tool.{name}.args_invalid`
    event (no invoke); on success, the Tool is called
    as before.

    The hook is OPTIONAL: existing callers that pass
    `args` explicitly continue to work unchanged.
    """

    def __init__(
        self,
        log: EventLog,
        registry: ToolRegistry,
        *,
        filter_fn: Optional[Callable[[Event], bool]] = None,
        pre_invoke_args_extractor: Optional[PreInvokeArgsExtractor] = None,
    ) -> None:
        self._log = log
        self._registry = registry
        self._filter = filter_fn or (lambda e: True)
        self._args_extractor = pre_invoke_args_extractor

    async def handle_request_event(self, request: Event) -> Result[Event, Exception]:
        """
        Handle a single `.requested` event: call the tool and
        emit the result. Returns the response event (or error).

        The body is a thin orchestrator: validate the
        event type, look up the tool, run the ACL
        check, then invoke + emit. Each phase lives in
        a private helper so the failure modes and the
        happy path stay easy to read.
        """
        # Phase 1: validate event type + extract tool name.
        parsed = self._parse_tool_request(request)
        if isinstance(parsed, Result):
            return parsed
        tool_name, error_message = parsed

        # Phase 2: resolve the tool from the registry.
        tool = self._registry.get(tool_name)
        if tool is None:
            return await self._emit_failure(
                request, f"Tool {tool_name!r} not registered"
            )

        # Phase 3: ACL check (ADR-017 Scenario B).
        acl_denial = self._check_acl(tool_name, request)
        if acl_denial is not None:
            return await self._emit_failure(request, acl_denial)

        # Phase 4: merge args + invoke + emit completion/failure.
        return await self._invoke_and_emit(request, tool, tool_name)

    def _parse_tool_request(
        self, request: Event
    ) -> "Result[tuple[str, str], None] | tuple[str, str]":
        """
        Validate that the request is a ``tool.<name>.requested``
        event and extract the tool name.

        Returns ``(tool_name, "")`` on success. On
        failure, returns ``Result.err(...)`` with a
        human-readable error message so the caller can
        short-circuit before any other work.

        The tool name may itself contain dots (e.g.
        ``invoice.issue``), so we slice off the leading
        ``tool.`` and trailing ``.requested`` rather
        than splitting on dots.
        """
        prefix = "tool."
        suffix = ".requested"
        et = request.event_type
        if not et.endswith(suffix):
            return Result.err(ValueError(f"Not a tool request: {request.event_type}"))
        if not et.startswith(prefix):
            return Result.err(
                ValueError(f"Malformed tool request: {request.event_type}")
            )
        tool_name = et[len(prefix) : -len(suffix)]
        if not tool_name:
            return Result.err(ValueError("Empty tool name in request"))
        return tool_name, ""

    def _check_acl(self, tool_name: str, request: Event) -> Optional[str]:
        """
        Run the tool-level ACL check (ADR-017 Scenario B).

        Returns ``None` when the call is allowed (no
        ACL configured, or ACL permits the principal).
        Returns a reason string when the call should
        be denied — the caller emits a ``failed`` event
        with that reason.

        The framework never raises on ACL failure
        mid-stream: the request event was already
        published, so we emit the failure to the
        EventLog and let the caller decide. Without a
        configured principal (e.g. unit tests that
        bypass auth), the call passes through — the
        ACL only applies when there is a principal to
        check against.
        """
        acl = self._registry.acl_for(tool_name)
        if acl is None:
            return None
        principal = principal_ctx.get()
        if principal is None:
            return None
        allowed, reason = acl.check(principal)
        if allowed:
            return None
        logger.warning(
            "tool_invoker.acl_denied",
            tool=tool_name,
            principal_agent_id=principal.agent_id,
            principal_role=principal.role.value,
            principal_tenant_id=principal.tenant_id,
            reason=reason,
        )
        return f"acl_denied: {reason}"

    async def _invoke_and_emit(
        self,
        request: Event,
        tool: Tool,
        tool_name: str,
    ) -> Result[Event, Exception]:
        """
        Merge args, invoke the tool, emit completion or
        failure. Returns the result event.
        """
        started = time.perf_counter()
        try:
            merged_args = await self._resolve_args(request, tool)
        except ArgsInvalid as e:
            return await self._emit_args_invalid(
                request,
                reason=str(e),
                missing=e.missing,
                type_mismatches=e.type_mismatches,
                unexpected=e.unexpected,
                latency_ms=ms_since(started),
            )

        # Inject the idempotency_key derived from the
        # request's event_id. It is stable across
        # re-dispatches: a reactive system that emits
        # the same request (e.g. after a dispatcher
        # restart) will produce the same key, and a
        # tool that honors it can dedupe the side
        # effect.
        try:
            invoke_result = await tool.invoke(
                idempotency_key=str(request.event_id),
                **merged_args,
            )
        except Exception as e:
            logger.exception(
                "tool.invoker.invoke_raised",
                tool=tool_name,
                error=str(e),
            )
            return await self._emit_failure(
                request,
                f"raised: {e!r}",
                latency_ms=ms_since(started),
            )
        latency_ms = ms_since(started)

        if invoke_result.is_ok():
            return await self._emit_completion(
                request,
                invoke_result.unwrap(),
                latency_ms=latency_ms,
            )
        return await self._emit_failure(
            request,
            str(invoke_result.err_value()),
            latency_ms=latency_ms,
        )

    async def _resolve_args(
        self,
        request: Event,
        tool: Tool,
    ) -> dict[str, "ToolArgValue"]:
        """
        Compute the kwargs dict for `tool.invoke`.

        ``ToolArgValue`` is the framework-level type for
        any value passed across the tool boundary. It
        is intentionally unbounded (``ToolArgValue = T``
        with no constraint) so concrete tools can
        specialise it via ``cast`` / explicit
        annotations inside their ``invoke`` bodies. The
        framework never inspects the values — it just
        passes them through.

        Returns a plain dict (NOT a ``Mapping`` proxy) so
        ``**merged_args`` works as expected.

        Raises `ArgsInvalid` if validation fails. The
        caller maps that to a `tool.{name}.args_invalid`
        event for the DLQ.
        """
        caller_args: dict[str, "ToolArgValue"] = dict(request.data or {})
        if self._args_extractor is None:
            # No hook: legacy path. We still validate
            # the caller's args against the schema so a
            # bad call surfaces as `args_invalid` even
            # when no extraction is configured. This
            # is a small behaviour change from "just
            # pass the kwargs through" — see ADR-013
            # §2.2 rationale. Tools that don't declare
            # a schema (None or {}) are unaffected.
            self._validate_or_raise(caller_args, tool.input_schema)
            return caller_args

        # Hook is configured: run extraction, merge,
        # validate. The extractor sees only the text
        # carried by the request (`data["text"]`).
        # Programmatic callers that do not supply a
        # text get the legacy path (no extraction,
        # validation only).
        text = (request.data or {}).get("text")
        if not text or not isinstance(text, str):
            self._validate_or_raise(caller_args, tool.input_schema)
            return caller_args

        extraction_result = await self._args_extractor(text, tool.name)
        if extraction_result.is_err():
            # The extractor itself failed (e.g. tool
            # not in registry, model crash). We do NOT
            # silently fall through: surface as
            # `args_invalid` so the operator sees it.
            err = extraction_result.err_value_or_raise()
            raise ArgsInvalid(
                f"extractor_error: {err!r}",
                missing=[],
                type_mismatches=[],
                unexpected=[],
            )

        extraction = extraction_result.unwrap()
        extracted_fields = dict(extraction.fields or {})

        # Merge policy: caller's args win.
        merged = {**extracted_fields, **caller_args}
        # Strip the bookkeeping key `text` from the
        # merged args — the Tool does not declare it
        # in its `input_schema` and forwarding it
        # would be flagged as `unexpected` by the
        # validator. The text lives in the request
        # event for audit / DLQ consumers.
        merged.pop("text", None)

        self._validate_or_raise(merged, tool.input_schema)
        return merged

    @staticmethod
    def _validate_or_raise(
        args: dict[str, "ToolArgValue"],
        schema: "Mapping[str, ToolArgValue] | None",
    ) -> None:
        """
        Run ``validate_args`` and translate
        ``SchemaValidationError`` into ``ArgsInvalid``.
        Centralises the try/except dance that
        previously appeared three times in
        ``_resolve_args`` (no-hook path, no-text path,
        merged-args path).
        """
        try:
            validate_args(args, schema)
        except SchemaValidationError as e:
            raise ArgsInvalid(
                str(e),
                missing=e.missing,
                type_mismatches=e.type_mismatches,
                unexpected=e.unexpected,
            ) from None

    async def run_once(
        self,
        agent_id: str,
    ) -> int:
        """
        Consumes pending `.requested` events for the agent
        and handles them. Returns the count handled.
        """
        events = await self._log.read(agent_id)
        seen_completed, seen_failed = self._index_results(events)
        return await self._process_pending_requests(events, seen_completed, seen_failed)

    @staticmethod
    def _index_results(
        events: list[Event],
    ) -> "tuple[set[str], set[str]]":
        """
        Walk the events list and return two sets:
        ``seen_completed`` and ``seen_failed``, each
        containing the str form of the
        `causation_id` (or `data["request_id"]`
        fallback) for every result event. The fallback
        is for legacy events that predate the
        ``causation_id`` convention.

        Separated from ``run_once`` so the
        indexing step is testable in isolation and
        the main loop stays flat.
        """
        seen_completed: set[str] = set()
        seen_failed: set[str] = set()
        for e in events:
            if e.event_type.endswith(".completed"):
                key = e.causation_id or e.data.get("request_id", "")
                seen_completed.add(str(key))
            elif e.event_type.endswith(".failed"):
                key = e.causation_id or e.data.get("request_id", "")
                seen_failed.add(str(key))
        return seen_completed, seen_failed

    async def _process_pending_requests(
        self,
        events: list[Event],
        seen_completed: set[str],
        seen_failed: set[str],
    ) -> int:
        """
        Walk the events list a second time and dispatch
        every `.requested` event whose id is NOT in
        `seen_completed` or `seen_failed` and that
        passes the configured filter. Returns the
        number of requests successfully handled.
        """
        handled = 0
        for e in events:
            if not e.event_type.endswith(".requested"):
                continue
            req_id = str(e.event_id)
            if req_id in seen_completed or req_id in seen_failed:
                continue
            if not self._filter(e):
                continue
            # Found a pending request; call the handler
            r = await self.handle_request_event(e)
            if r.is_ok():
                handled += 1
        return handled

    # ------------------------------------------------------------------ emit
    #
    # Thin instance-method wrappers around the module-level
    # helpers in `_emit.py`. They forward ``self._log`` so the
    # call-sites read as ``self._emit_*`` instead of having to
    # pass the log every time. The free helpers already turn a
    # Result.err from the EventLog into an Exception; here we
    # re-wrap that into a ``Result.err`` so ``handle_request_event``
    # propagates it to the caller unchanged.

    async def _emit_completion(
        self,
        request: Event,
        result: "ToolArgValue",
        *,
        latency_ms: float,
    ) -> Result[Event, Exception]:
        try:
            e = await emit_completion(
                self._log,
                request,
                result,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            return Result.err(exc)
        return Result.ok(e)

    async def _emit_args_invalid(
        self,
        request: Event,
        *,
        reason: str,
        missing: list[str],
        type_mismatches: list[tuple[str, str, str]],
        unexpected: list[str],
        latency_ms: Optional[float] = None,
    ) -> Result[Event, Exception]:
        try:
            e = await emit_args_invalid(
                self._log,
                request,
                reason=reason,
                missing=missing,
                type_mismatches=type_mismatches,
                unexpected=unexpected,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            return Result.err(exc)
        return Result.ok(e)

    async def _emit_failure(
        self,
        request: Event,
        error_message: str,
        *,
        latency_ms: Optional[float] = None,
    ) -> Result[Event, Exception]:
        try:
            e = await emit_failure(
                self._log,
                request,
                error_message,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            return Result.err(exc)
        return Result.ok(e)
