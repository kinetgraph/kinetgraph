# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
LiteLLMTool — generic LLM completion tool via LiteLLM.

Camada 1 (Tool de I/O): sabe falar com qualquer provider
suportado por LiteLLM (OpenAI, Anthropic, Google, Mistral,
Ollama local, etc). NÃO sabe o que está sendo perguntado —
isso é papel dos `roles/`.

`LLMTransport`, `LLMResponse`, `LLMChunk`, `LLMUsage` são
contratos do framework (`kntgraph.tools.llm_transport`)
e são re-exportados daqui por conveniência. Esta Tool
implementa `LiteLLMTransportAdapter` (a chamada real ao
`litellm.acompletion`) e o `LiteLLMTool` (orquestração:
fallback, rate limit, circuit breaker, budget, streaming).

Decisões de design
------------------

  1. **Schema explícito**: `system`, `user`, `model`,
     `temperature`, `max_tokens`, `response_format`, `stream`.
     Tudo keyword-only. `**kwargs` é absorvido para forward
     de params LiteLLM-específicos (top_p, stop, etc).

  2. **Idempotency**: herda o contrato do framework
     (`idempotency_key` keyword). LiteLLM não dedupe por si;
     a Tool NÃO cacheia — quem cacheia é o caller (Role) ou
     um wrapper externo. LiteLLMTool é puramente a ponte.

  3. **Fallback chain**: se o modelo primário falhar com
     `RateLimitError`, tenta o próximo em `fallback_models`.
     Outros erros (4xx, validation) NÃO disparam fallback —
     são retornados como `Err`.

  4. **Rate limit e cost budget**: opcionais. Quando
     configurados, são checados **antes** da chamada. Se
     recusarem, retornam `Err(ToolError("rate_limited"))` ou
     `Err(ToolError("budget_exhausted"))` — sem chamar o
     provider.

  5. **Output padronizado**: `LLMResponse` com `text`,
     `usage`, `model`, `latency_ms`, `cost_usd`. Custo é
     extraído de `litellm.completion_cost(response)` se
     disponível, senão `None`.

  6. **Timeout**: `asyncio.wait_for` ao redor da chamada.
     Timeout retorna `Err(ToolError("timeout"))`.

  7. **Drop unsupported params**: quando True, params não
     suportados pelo modelo são silenciosamente dropados
     pelo LiteLLM (configurado via `litellm.drop_params=True`).
     Útil em multi-provider onde nem toda feature existe.

Streaming: a interface atual é síncrona (`complete` retorna
`LLMResponse`). Para streaming, use a função `astream` que
retorna um `AsyncIterator[LLMChunk]`. Ambos vivem no mesmo
objeto, ambos honram `idempotency_key` (caller decide).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Optional

import structlog

from kntgraph.core.result import (
    Err,
    Ok,
    Result,
    ToolError,
)
from kntgraph.tools.llm_transport import (
    LLMChunk,
    LLMRequest,
    LLMResponse,
    LLMTransport,
    LLMUsage,
)
from kntgraph.tools.worker import tool_worker


logger = structlog.get_logger()


# -----------------------------------------------------------------------------
# Stream sentinels (used by `LiteLLMTool.astream`)
# -----------------------------------------------------------------------------
# Returned by `_next_chunk_with_timeout` to signal end-of-
# stream and timeout respectively. Using unique objects
# (not Optional / sentinel-strings) makes the call site's
# intent obvious and avoids `None` collision with
# legitimately empty chunks (a chunk `Result[LLMChunk,
# ToolError]` is never `None`).


class _StreamDone:
    """Sentinel: the async iterator is exhausted."""

    _instance: "_StreamDone | None" = None

    def __new__(cls) -> "_StreamDone":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "_STREAM_DONE"


class _StreamTimeout:
    """Sentinel: the chunk did not arrive before the deadline."""

    _instance: "_StreamTimeout | None" = None

    def __new__(cls) -> "_StreamTimeout":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "_STREAM_TIMEOUT"


_STREAM_DONE = _StreamDone()
_STREAM_TIMEOUT = _StreamTimeout()


# -----------------------------------------------------------------------------
# Cost computation helper
# -----------------------------------------------------------------------------
# Cost computation helper
# -----------------------------------------------------------------------------


def _compute_cost_usd(response: dict) -> Optional[float]:
    """
    Best-effort cost extraction. LiteLLM has `completion_cost`
    but it requires the model to be in its pricing DB. Local
    models (Ollama) return None.

    Iter 22: when ``completion_cost`` returns ``None`` (or
    raises) AND the response carries an explicit ``_cost_usd``
    field, fall back to that value. The underscore prefix
    marks it as a transport-side convention (set by
    ``FakeLLMTransport`` and similar test doubles). Production
    LiteLLM responses do not set ``_cost_usd`` — the field is
    reserved for fakes that want a deterministic cost without
    monkey-patching the LiteLLM pricing DB.
    """
    try:
        import litellm

        computed = float(litellm.completion_cost(completion_response=response))
        return computed
    except Exception as exc:
        logger.debug("llm.compute_cost_fallback", error=str(exc))
    # Fallback for transport-side explicit cost.
    fallback = response.get("_cost_usd")
    if isinstance(fallback, (int, float)):
        return float(fallback)
    return None


# -----------------------------------------------------------------------------
# LiteLLMTool
class LiteLLMTransportAdapter(LLMTransport):
    """
    Default transport: calls `litellm.acompletion`. Requires
    the `litellm` package.

    Iter 18c (ADR-019 epílogo): renamed from
    ``LiteLLMTransportAdapter``. The new name follows the
    adapter convention (RedisEventLogAdapter,
    FalkorDBGraphAdapter, OllamaEmbeddingAdapter).

    Most callers should NOT construct this directly —
    use ``LLMClient`` (the facade) which holds a
    reference to a low-level adapter and delegates
    every call.

    Iter 28 FU 3: the method is now ``__call__`` (not
    ``complete``). The transport IS-A
    ``Callable[LLMRequest, dict]`` by structural typing.
    """

    async def __call__(
        self,
        request: "LLMRequest",
    ) -> dict:
        import litellm

        litellm.drop_params = request.drop_unsupported_params
        completion_kwargs = self._build_completion_kwargs(request)
        try:
            response: Any = await litellm.acompletion(**completion_kwargs)
        except litellm.RateLimitError as e:
            # Provider 429. Translate to typed LLM
            # exception so the fallback loop can switch
            # to the next model in `fallback_models``.
            raise LLMRateLimitError(str(e)) from e
        except litellm.AuthenticationError as e:
            # Provider 401/403. Non-recoverable.
            raise LLMAuthError(str(e)) from e
        except litellm.APIError as e:
            # Generic provider error. Surface as a
            # typed `LLMError`; the fallback loop
            # treats this as non-recoverable.
            raise LLMError(str(e)) from e
        return self._dump_response(response)

    def _build_completion_kwargs(self, request: "LLMRequest") -> dict[str, Any]:
        """Build the ``litellm.acompletion`` kwargs
        from the ``LLMRequest`` value object. The
        ``response_format`` slot is forwarded to
        ``litellm_params`` (litellm's per-request
        escape hatch) so it survives the
        ``drop_unsupported_params`` filter. The
        request's ``extra`` dict is merged last so
        callers can override defaults via
        ``LLMRequest(extra={"...": ...})``."""
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_format is not None:
            extra_dict = request.extra if isinstance(request.extra, dict) else {}
            litellm_params_raw = extra_dict.get("litellm_params", {})
            litellm_params: dict[str, object] = (
                dict(litellm_params_raw) if isinstance(litellm_params_raw, dict) else {}
            )
            # ``response_format`` is typed as ``JsonValue``
            # in the framework (covers dict-shaped JSON
            # schemas as well as primitives); litellm
            # expects a dict or Pydantic model, so the
            # cast is a runtime no-op for callers that
            # pass a JSON schema.
            litellm_params["response_format"] = request.response_format  # type: ignore[assignment]
            request = replace(
                request,
                extra={
                    **extra_dict,
                    "litellm_params": litellm_params,
                },
            )
        kwargs.update(request.extra)
        return kwargs

    def _dump_response(self, response: Any) -> dict:
        """Coerce the litellm ``ModelResponse`` (or
        a fallback shape) into a JSON-serialisable
        dict. The runtime type is ``ModelResponse``
        in the ``acompletion`` path
        (``CustomStreamWrapper`` is reserved for the
        ``astream`` path, which this adapter does
        not exercise). ``model_dump`` is the
        canonical serialiser; we fall back to a
        raw dict copy, then to a ``{"_repr": ...}``
        sentinel so the response is always
        JSON-serialisable (some custom transports
        return exotic objects)."""
        dump = getattr(response, "model_dump", None)
        if callable(dump):
            try:
                return response.model_dump()
            except Exception as exc:
                logger.debug("llm.transport_model_dump_failed", error=str(exc))
        if isinstance(response, dict):
            return dict(response)
        return {"_repr": repr(response)}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


class _RateLimitLike(Exception):
    """Deprecated: use `LLMRateLimitError` instead.

    Kept as an alias for the test fake (`_FakeRateLimitError`),
    which subclasses this for backwards compatibility. New code
    should raise/catch `LLMRateLimitError` directly.
    """


class _AuthLike(Exception):
    """Deprecated: use `LLMAuthError` instead.

    Kept as an alias for the test fake (`_FakeAuthError`),
    which subclasses this for backwards compatibility. New code
    should raise/catch `LLMAuthError` directly.
    """


# -----------------------------------------------------------------------------
# Typed LLM exceptions
# -----------------------------------------------------------------------------
# These are the exceptions the LLM tool actually raises. They
# are caught by the fallback loop in `LiteLLMTool.invoke()`
# to decide between (a) trying the next model in the
# `fallback_models` chain (rate limit) and (b) aborting
# immediately (auth). Generic LLM errors do not trigger
# fallback — they are returned as `Err(ToolError(...))`.


class LLMError(Exception):
    """Base for LLM tool errors.

    `LiteLLMTransportAdapter` translates provider-specific exceptions
    (`litellm.APIError`, `openai.RateLimitError`, etc.) into
    one of the typed subclasses below. The fallback loop in
    `LiteLLMTool.invoke()` uses `isinstance` to distinguish
    recoverable (rate limit) from non-recoverable (auth, generic)
    failures.
    """


class LLMRateLimitError(LLMError):
    """Provider returned 429 (or equivalent).

    The fallback loop in `LiteLLMTool.invoke()` catches this
    and tries the next model in `fallback_models`.
    """


class LLMAuthError(LLMError):
    """Provider rejected credentials (401, 403, etc).

    Non-recoverable: the fallback loop in
    `LiteLLMTool.invoke()` aborts immediately on this
    exception (auth errors are not fixed by switching
    models).
    """


class _TerminalToolError(ToolError):
    """
    Marker subclass of `ToolError` for errors that the
    fallback loop in `LiteLLMTool.invoke()` must NOT
    retry against the next model in `fallback_models`.

    Used for:
      - `LLMAuthError` (bad credentials do not fix
        themselves by switching model).
      - Generic transport errors (`llm_error`) where
        we don't have enough signal to know if a
        different model would behave differently;
        retrying tends to amplify problems.

    Recoverable errors (timeout, rate limit, circuit
    open) stay as the base `ToolError` — the fallback
    loop keeps iterating.
    """


def _safe_dict(obj: Any) -> dict:
    """
    Best-effort conversion of an arbitrary object to a
    plain dict. Used as a fallback when neither
    `model_dump()` nor `dict(obj)` is appropriate.
    """
    if isinstance(obj, dict):
        return dict(obj)
    out: dict = {}
    for attr in dir(obj):
        if attr.startswith("_"):
            continue
        try:
            v = getattr(obj, attr)
            if callable(v):
                continue
            out[attr] = v
        except Exception as exc:
            logger.debug("llm.safe_dict_attr_failed", attr=attr, error=str(exc))
    return out


def _to_llm_response(
    completion: Any, requested_model: str, latency_ms: float
) -> LLMResponse:
    """
    Build LLMResponse from a LiteLLM completion dict.

    LiteLLM returns a `ModelResponse` (dict-like) with
    `.choices[0].message.content` and `.usage`. We accept
    both dict and pydantic shapes.
    """
    text, finish = _parse_message(completion)
    usage = _parse_usage(completion)
    model = completion.get("model") or requested_model
    cost = _compute_cost_usd(completion)
    raw = _convert_to_raw_dict(completion)
    return LLMResponse(
        text=text,
        model=model,
        usage=usage,
        latency_ms=latency_ms,
        cost_usd=cost,
        finish_reason=finish,
        raw=raw,
    )


def _parse_message(
    completion: Any,
) -> "tuple[str, Optional[str]]":
    """
    Extract ``text`` and ``finish_reason`` from the
    first choice of the completion. The first choice
    may be absent (some providers return an empty
    `choices` list on certain error paths), in which
    case the text is empty and finish is None.
    """
    choices = completion.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    text = message.get("content") or ""
    finish = first.get("finish_reason")
    return text, finish


def _parse_usage(completion: Any) -> LLMUsage:
    """
    Build an ``LLMUsage`` from the completion's
    ``usage`` block. Missing fields default to 0
    (the standard LiteLLM shape).
    """
    usage_raw = completion.get("usage") or {}
    return LLMUsage(
        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        total_tokens=int(usage_raw.get("total_tokens") or 0),
    )


def _convert_to_raw_dict(completion: Any) -> dict:
    """
    Convert a LiteLLM completion to a plain dict for
    storage. LiteLLM returns a pydantic
    ``ModelResponse`` (which has ``model_dump()``);
    some callers pre-convert to dict; everything
    else falls back to ``_safe_dict`` (a defensive
    attribute-by-attribute copy).

    The try/except is intentional: ``model_dump``
    can fail on pydantic validation errors that
    only surface when the model is actually
    serialised (not on construction). In that case
    we degrade to ``_safe_dict`` rather than
    propagating the failure to the caller.
    """
    if hasattr(completion, "model_dump"):
        try:
            return completion.model_dump()
        except Exception:
            return _safe_dict(completion)
    if isinstance(completion, dict):
        return dict(completion)
    return _safe_dict(completion)


async def _astream_litellm_inner(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    **kwargs: Any,
) -> AsyncIterator[Result[LLMChunk, ToolError]]:
    """
    Inner async-generator that yields Result[LLMChunk]
    from LiteLLM streaming. Separated from the timeout
    wrapper so the timeout can be applied to the call
    that initiates the stream (not to the iterator
    itself, which is a coroutine-generator and cannot
    be passed to asyncio.wait_for).
    """
    import litellm

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        response: Any = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            **kwargs,
        )
        # The stream wrapper is an AsyncIterator at runtime
        # (CustomStreamWrapper implements __aiter__), but
        # the static type is ModelResponse. Cast to Any
        # to silence the type checker without disabling
        # checks elsewhere.
        async for chunk in response:
            choices = chunk.get("choices") or []
            first = choices[0] if choices else {}
            delta = (first.get("delta") or {}).get("content") or ""
            finish = first.get("finish_reason")
            yield Ok(
                LLMChunk(
                    delta=delta,
                    model=chunk.get("model", model),
                    finish_reason=finish,
                )
            )
    except Exception as e:
        yield Err(ToolError(f"stream_error: {e!r}"))

    litellm.drop_params = True
    os.environ.setdefault("LITELLM_TELEMETRY", "False")


# -----------------------------------------------------------------------------
# LiteLLMToolWorker (ADR-043)
# -----------------------------------------------------------------------------


@tool_worker(
    name="chat_llm",
    description="Generic LLM completion via LiteLLM (Ollama, OpenAI, Anthropic, Google, etc.).",
    max_concurrency=10,
    retries=3,
)
class LiteLLMToolWorker:
    """
    Migrated LLM bridge: ADR-036 worker pattern (ADR-043).

    The legacy ``LiteLLMTool`` is on the ``Tool`` (Protocol)
    path. It runs in the dispatcher process and blocks the
    dispatcher's event loop while the LLM responds. This
    class is the **canonical** path going forward: it
    runs in the ``WorkerManager``'s
    ``ProcessPoolExecutor`` and supports cross-tick
    correlation via ``causation_id`` (= the
    ``request_event_id`` of the
    ``tool.chat_llm.requested`` event).

    Wire contract
    -------------

    The worker is invoked by the ``WorkerManager`` with
    these keyword arguments (extracted from the
    ``tool.chat_llm.requested`` event's data via the
    ``request_tool`` helper):

      - ``system`` (str, required): the system prompt.
      - ``user`` (str, required): the user message.
      - ``model`` (str, optional): LiteLLM model name
        (e.g. ``"ollama/qwen3.5:4b"``,
        ``"openai/gpt-4o-mini"``). Default read from
        env (``LLM_DEFAULT_MODEL``).
      - ``temperature`` (float, optional).
      - ``max_tokens`` (int, optional).
      - ``response_format`` (dict, optional): JSON
        schema for structured output.
      - ``think`` (bool, optional): for thinking
        models (Ollama qwen3.5).
      - ``idempotency_key`` (str, required): the
        ``request_event_id`` (injected by the
        ``WorkerManager``).

    Return envelope
    ---------------

    The worker returns a JSON-serialisable dict
    (the result crosses the process boundary via
    the ``WorkerManager``'s ``_invoke_tool_sync``):

      - ``text`` (str): the assistant reply.
      - ``model`` (str): the model that actually
        responded (LiteLLM may substitute on
        fallback).
      - ``usage`` (dict): ``{"prompt_tokens": int,
        "completion_tokens": int, "total_tokens":
        int}``.
      - ``finish_reason`` (str | None): ``"stop"``,
        ``"length"``, ``"tool_calls"``, etc.
      - ``cost_usd`` (float | None): the cost of
        the call (if calculable).
      - ``latency_ms`` (float): the wall-clock
        latency of the LLM call.

    Idempotency
    -----------

    The worker does NOT dedupe by itself. The
    ``WorkerManager`` does (via the
    ``xpending`` / ``xautoclaim`` path), so a
    retry (e.g. consumer-group rebalance) does
    not produce duplicate LLM calls. The
    ``idempotency_key`` is passed for downstream
    consumers (e.g. the example 05b) that may
    want to memoize the response.
    """

    def __init__(self) -> None:
        """
        Build a default-configured LLM worker.

        The configuration is read from the environment
        (see ``LLMConfig.from_env``). The
        ``ProcessPoolExecutor`` worker runs this
        ``__init__`` once per process (the same
        process is reused across many
        ``invoke`` calls via the pool).
        """
        from kntgraph.agents.config import LLMConfig

        cfg = LLMConfig.from_env()
        self._default_model = cfg.default_model
        # ``LLMConfig`` does not own
        # ``temperature`` / ``max_tokens``; those
        # are caller decisions (and per-call kwargs
        # in this worker). We hold ``timeout_s``
        # from the config (a worker-level
        # invariant) and read the others from
        # ``LLMSettings`` directly.
        self._timeout_s = cfg.timeout_s
        # Lazily-initialised transport (avoids the
        # ``litellm`` import cost in the parent
        # process; the worker process is a fresh
        # interpreter anyway).
        self._transport: "LLMTransport | None" = None

    def _get_transport(self) -> "LLMTransport":
        if self._transport is None:
            self._transport = LiteLLMTransportAdapter()
        return self._transport

    async def invoke(
        self,
        system: str,
        user: str,
        *,
        idempotency_key: str,
        model: "str | None" = None,
        temperature: "float | None" = None,
        max_tokens: "int | None" = None,
        think: bool = False,
        response_format: "dict | None" = None,
        stream: bool = False,
    ) -> "Result[dict[str, Any], ToolError]":
        """
        Run a single LLM completion via the
        ``LiteLLMTransportAdapter`` and return the
        result as a JSON-serialisable dict.

        The result envelope is documented in the
        class docstring. On any transport error
        (rate limit, auth, timeout, etc.) the
        worker returns ``Err(ToolError(...))``; the
        ``WorkerManager`` translates that into a
        ``tool.chat_llm.failed`` event. The
        original exception is preserved as
        ``__cause__`` for diagnostics.
        """
        effective_model = model or self._default_model
        # ``temperature`` and ``max_tokens`` are
        # caller decisions (per-call kwargs). The
        # worker does not impose defaults — LiteLLM's
        # own per-model defaults apply when the kwarg
        # is ``None``.
        effective_temperature = temperature
        effective_max_tokens = max_tokens

        transport = self._get_transport()
        request = LLMRequest(
            model=effective_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=effective_temperature,
            max_tokens=effective_max_tokens,
            response_format=response_format,
            drop_unsupported_params=True,
            idempotency_key=idempotency_key,
            extra={"think": think, "stream": stream},
        )
        try:
            started = time.perf_counter()
            completion = await asyncio.wait_for(
                transport(request),
                timeout=self._timeout_s,
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
        except asyncio.TimeoutError as e:
            err = ToolError(f"llm_timeout after {self._timeout_s}s")
            err.__cause__ = e
            return Err(err)
        except Exception as e:
            err = ToolError(f"llm_transport_error: {e!r}")
            err.__cause__ = e
            return Err(err)

        # Translate the transport's dict into the
        # worker's public envelope.
        text, finish_reason = _parse_message(completion)
        usage_raw = completion.get("usage") or {}
        return Ok(
            {
                "text": text,
                "model": completion.get("model") or effective_model,
                "usage": {
                    "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage_raw.get("completion_tokens") or 0),
                    "total_tokens": int(usage_raw.get("total_tokens") or 0),
                },
                "finish_reason": finish_reason,
                "cost_usd": _compute_cost_usd(completion),
                "latency_ms": latency_ms,
            }
        )


# -----------------------------------------------------------------------------
