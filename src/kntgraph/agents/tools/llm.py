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
contratos do framework (`kntgraph.agents.tools.llm_transport`)
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
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Optional

from kntgraph.core.result import (
    BusinessError,
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
from kntgraph.resilience import (
    BackoffPolicy,
    CircuitBreaker as ResilienceCircuitBreaker,
    with_timeout_and_retry,
)
from ..config import CostBudget, RateLimiter


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
    except Exception:
        pass
    # Fallback for transport-side explicit cost.
    fallback = response.get("_cost_usd")
    if isinstance(fallback, (int, float)):
        return float(fallback)
    return None


# -----------------------------------------------------------------------------
# LiteLLMTool
# -----------------------------------------------------------------------------


class LiteLLMTool:
    """
    Tool genérica de LLM completion.

    Registrável no `ToolRegistry` do framework. A assinatura
    `invoke(*, idempotency_key, system, user, ...)` satisfaz
    o Protocol `Tool` (após o ADR-005).

    NÃO cacheia. NÃO roteia por papel. NÃO conhece prompt
    do domínio. Esses são responsabilidade dos `roles/`.

    Veja ADR-007 para a decisão de usar LiteLLM e os trade-offs.
    """

    name = "llm.complete"
    description = (
        "Generic LLM completion via LiteLLM. Supports OpenAI, "
        "Anthropic, Google, Mistral, Ollama, and any LiteLLM "
        "provider. Returns text + usage + cost."
    )
    input_schema: dict = {
        "type": "object",
        "required": ["system", "user"],
        "properties": {
            "system": {"type": "string"},
            "user": {"type": "string"},
            "model": {"type": "string"},
            # Iter 22: defaults are no longer hardcoded
            # in the schema. The effective default is read
            # from Settings (``llm_default_temperature`` /
            # ``llm_default_max_tokens``) at runtime. The
            # schema simply marks the parameter as optional.
            "temperature": {"type": "number"},
            "max_tokens": {"type": "integer"},
            "response_format": {"type": "object"},
            "stream": {"type": "boolean", "default": False},
        },
    }

    @staticmethod
    def _resolve_llm_defaults(
        *,
        default_model: "str | None",
        temperature: "float | None",
        max_tokens: "int | None",
        timeout_s: "float | None",
        max_cost_usd_per_request: "float | None",
    ) -> "tuple[str, float, int, float, float]":
        """
        Resolve the effective ``default_model``,
        ``temperature``, ``max_tokens``, ``timeout_s``
        and ``max_cost_usd_per_request`` from explicit
        args + Settings.

        The sentinel ``None`` means "no override; use
        Settings". Any explicit value wins. Extracted
        so the ``__init__`` body stays flat (CC ≤ 2)
        and the defaults are easy to test in isolation.

        All Settings access for the LLM Tool is
        encapsulated in this single helper. New
        Settings fields can be added here without
        changing the ``__init__`` shape.
        """
        from kntgraph.infra.config import fresh_settings

        _s = fresh_settings()
        effective_model = (
            default_model if default_model is not None else _s.llm_default_model
        )
        effective_temperature = (
            temperature if temperature is not None else _s.llm_default_temperature
        )
        effective_max_tokens = (
            max_tokens if max_tokens is not None else _s.llm_default_max_tokens
        )
        effective_timeout = timeout_s if timeout_s is not None else _s.llm_timeout
        effective_cost_cap = (
            max_cost_usd_per_request
            if max_cost_usd_per_request is not None
            else _s.llm_max_cost_usd_per_request
        )
        return (
            effective_model,
            effective_temperature,
            effective_max_tokens,
            effective_timeout,
            effective_cost_cap,
        )

    def __init__(
        self,
        *,
        default_model: Optional[str] = None,
        fallback_models: Optional[list[str]] = None,
        rate_limiter: Optional[RateLimiter] = None,
        cost_budget: Optional[CostBudget] = None,
        timeout_s: Optional[float] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_cost_usd_per_request: Optional[float] = None,
        drop_unsupported_params: bool = True,
        transport: Optional["LLMTransport"] = None,
        circuit_breaker: Optional[ResilienceCircuitBreaker] = None,
        retry_attempts: int = 0,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 10.0,
        retry_max_total_seconds: float = 60.0,
    ) -> None:
        """
        `circuit_breaker` is an optional
        `kntgraph.resilience.CircuitBreaker` instance.
        When set, every `_call_litellm` is wrapped and
        failures count toward the breaker's threshold.
        The breaker's `call` returns `Result[Any, BusinessError]`;
        we map `Err` back into a `ToolError` so the existing
        fallback loop in `invoke()` continues to work.

        `retry_attempts`, `retry_base_delay`, `retry_max_delay`,
        `retry_max_total_seconds` configure the per-call
        retry loop (powered by `with_timeout_and_retry`).
        When `retry_attempts=0` (default), no retries are
        attempted; the existing fallback chain in
        `invoke()` covers that case. When `retry_attempts >= 1`,
        each `_call_litellm` is retried with exponential
        backoff + jitter before falling back to the next
        model. `retry_max_total_seconds` is the absolute
        budget across all retries (caps the worker time
        a single bad call can pin).

        Iter 22: ``temperature``, ``max_tokens`` and
        ``max_cost_usd_per_request`` are sampled from
        ``Settings`` (``llm_default_temperature``,
        ``llm_default_max_tokens``,
        ``llm_max_cost_usd_per_request``) when the
        caller leaves them as ``None``. The sentinel
        ``None`` means "use Settings"; an explicit value
        wins (per-tool override).

        ``max_cost_usd_per_request=0`` disables the cap
        entirely. Setting it to ``None`` reads the cap
        from Settings (``1.0`` by default).
        """
        # Iter 20 + 22: read defaults from Settings via
        # the encapsulated helper. The caller can still
        # override per-tool by passing the value
        # explicitly. The sentinel ``None`` means
        # "no override; use Settings".
        (
            self._default_model,
            self._default_temperature,
            self._default_max_tokens,
            self._timeout_s,
            self._max_cost_usd_per_request,
        ) = self._resolve_llm_defaults(
            default_model=default_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            max_cost_usd_per_request=max_cost_usd_per_request,
        )
        self._fallback = list(fallback_models or [])
        self._rate_limiter = rate_limiter
        self._cost_budget = cost_budget
        self._drop = drop_unsupported_params
        # Transport is the pluggable I/O boundary. When None,
        # the tool creates a `LiteLLMTransportAdapter` lazily (requires
        # `litellm` installed). Tests inject a fake transport.
        self._transport = transport
        # Optional circuit breaker. When set, all calls go
        # through `breaker.call(...)` and failures count
        # toward the breaker's threshold.
        self._circuit_breaker: Optional[ResilienceCircuitBreaker] = circuit_breaker
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._retry_max_total_seconds = retry_max_total_seconds

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def fallback_models(self) -> tuple[str, ...]:
        return tuple(self._fallback)

    # ------------------------------------------------------------------ I/O

    async def invoke(
        self,
        *,
        idempotency_key: str,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict] = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> Result[LLMResponse, ToolError]:
        """
        Single-shot completion. Returns `LLMResponse` on Ok.

        `idempotency_key` is the framework contract (ADR-005).
        This tool does NOT use it to dedupe (LiteLLM has no
        server-side cache); the caller (Role) is responsible
        for caching if desired. The key is accepted and
        ignored — present for Protocol conformance.

        `response_format` is a JSON schema dict; LiteLLM
        forwards it to providers that support structured
        output. With `drop_unsupported_params=True`,
        providers that don't support it ignore the param.

        Iter 22: ``temperature`` and ``max_tokens`` default
        to ``None`` (sentinel: "use Settings"). When the
        caller passes an explicit value, that value wins
        per-call. The effective values are forwarded to
        the transport.
        """
        if stream:
            return Err(
                ToolError(
                    "use astream() for streaming; invoke() returns the full response"
                )
            )

        effective_temperature, effective_max_tokens = self._effective_kwargs(
            temperature, max_tokens
        )

        if (
            err := await self._check_pre_call(user, system, effective_max_tokens)
        ) is not None:
            return err

        models = [model or self._default_model] + self._fallback
        last_err: Optional[ToolError] = None
        for m in models:
            r = await self._try_one_model(
                model=m,
                system=system,
                user=user,
                temperature=effective_temperature,
                max_tokens=effective_max_tokens,
                response_format=response_format,
                idempotency_key=idempotency_key,
                **kwargs,
            )
            if r.is_ok():
                response = r.unwrap()
                charge_err = await self._charge_and_cap_cost(response)
                if charge_err is not None:
                    return Err(charge_err)
                return Ok(response)
            err = r.err_value_or_raise()
            # Auth / generic transport errors are not
            # recoverable by trying the next model. Other
            # failures (timeout, rate limit, circuit open)
            # are recoverable — keep iterating through
            # `fallback_models`.
            if isinstance(err, _TerminalToolError):
                return Err(err)
            last_err = err
        return Err(last_err or ToolError("all_models_failed"))

    @staticmethod
    def _resolve_per_call_kwargs(
        explicit_temperature: Optional[float],
        explicit_max_tokens: Optional[int],
        default_temperature: float,
        default_max_tokens: int,
    ) -> "tuple[float, int]":
        """
        Resolve per-call ``temperature`` and ``max_tokens``:
        explicit value wins; otherwise falls back to the
        tool's resolved default (which itself came from
        Settings). Extracted so the same resolution is
        shared by ``invoke()`` and ``astream()``.
        """
        effective_temperature = (
            explicit_temperature
            if explicit_temperature is not None
            else default_temperature
        )
        effective_max_tokens = (
            explicit_max_tokens
            if explicit_max_tokens is not None
            else default_max_tokens
        )
        return effective_temperature, effective_max_tokens

    def _effective_kwargs(
        self, temperature: Optional[float], max_tokens: Optional[int]
    ) -> "tuple[float, int]":
        """
        Instance binding of ``_resolve_per_call_kwargs`` that
        uses this tool's resolved defaults.
        """
        return self._resolve_per_call_kwargs(
            explicit_temperature=temperature,
            explicit_max_tokens=max_tokens,
            default_temperature=self._default_temperature,
            default_max_tokens=self._default_max_tokens,
        )

    async def _charge_and_cap_cost(self, response: "LLMResponse") -> "ToolError | None":
        """
        Post-call cost handling: charge the configured
        ``cost_budget`` (when set and the response carries
        a known cost) and enforce the per-request
        ``max_cost_usd_per_request`` cap. Returns
        ``None`` to propagate the response, or a
        ``ToolError`` when the cap rejects the call.
        Callers wrap the return value in ``Err(...)``.

        Iter 22: the cap is a hard ceiling — calls above
        it are rejected regardless of whether the
        ``cost_budget`` would have allowed them. When the
        cap is disabled (``0``) or the cost is unknown
        (``None``), the call passes through.
        """
        if self._cost_budget is not None and response.cost_usd:
            await self._cost_budget.charge(response.cost_usd)
        if self._max_cost_usd_per_request > 0:
            cost = response.cost_usd
            if cost is not None and cost > self._max_cost_usd_per_request:
                return ToolError(
                    f"cost_cap_exceeded: response "
                    f"reported {cost} USD, cap is "
                    f"{self._max_cost_usd_per_request} USD"
                )
        return None

    async def _check_pre_call(
        self, user: str, system: str, max_tokens: int
    ) -> Optional[Err[ToolError]]:
        """
        Pre-flight checks shared by all model attempts:
        rate limit, then cost budget. Returns `Err` on the
        first refusal or `None` to proceed.
        """
        if self._rate_limiter is not None:
            if not await self._rate_limiter.allow():
                return Err(ToolError("rate_limited"))
        if self._cost_budget is not None:
            # Rough estimate: $0.001 per 1k tokens.
            # Conservative; real cost is computed post-call
            # and charged against the same budget.
            est_tokens = len(user) // 4 + len(system) // 4 + max_tokens
            est_cost = (est_tokens / 1000.0) * 0.001
            if not await self._cost_budget.can_spend(est_cost):
                return Err(ToolError("budget_exhausted"))
        return None

    async def _try_one_model(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        response_format: Optional[dict],
        idempotency_key: str,
        **kwargs: Any,
    ) -> Result[LLMResponse, ToolError]:
        """
        Attempt the call against a single model, wrapped in
        a hard timeout. Translates typed transport / breaker
        errors into role-friendly `ToolError` shapes:

          - `asyncio.TimeoutError` → recoverable
            (`ToolError("timeout after {timeout_s}s")`) —
            try next.
          - `LLMRateLimitError` → recoverable — try next.
          - `LLMAuthError` → **terminal** (`_TerminalToolError`)
            — return immediately.
          - `BusinessError` (breaker rejection) →
            recoverable — try next.
          - `Exception` (other transport errors) →
            **terminal** (`_TerminalToolError`).

        The terminal/recoverable distinction is encoded
        in the error *type* (`_TerminalToolError` vs
        `ToolError`), not in a string prefix. The fallback
        loop in `invoke()` uses `isinstance` to decide
        whether to keep iterating through
        `fallback_models`.
        """
        try:
            response = await asyncio.wait_for(
                self._call_litellm(
                    model=model,
                    system=system,
                    user=user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    idempotency_key=idempotency_key,
                    **kwargs,
                ),
                timeout=self._timeout_s * (self._retry_attempts + 1),
            )
        except asyncio.TimeoutError:
            return Err(ToolError(f"timeout after {self._timeout_s}s"))
        except LLMRateLimitError as e:
            return Err(ToolError(f"rate_limit on {model}: {e}"))
        except LLMAuthError as e:
            return Err(_TerminalToolError(f"auth_error on {model}: {e}"))
        except BusinessError as e:
            # Circuit breaker (when configured) wraps the
            # transport call through `breaker.call()` which
            # returns `Err(BusinessError(...))` on rejection
            # (OPEN) or on the inner call's failure.
            # `_call_litellm` re-raises so we can switch to
            # the next model — same treatment as a rate
            # limit.
            return Err(ToolError(f"circuit_open on {model}: {e}"))
        except Exception as e:
            return Err(_TerminalToolError(f"llm_error on {model}: {e!r}"))
        return Ok(response)

    async def astream(
        self,
        *,
        idempotency_key: str,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Result[LLMChunk, ToolError]]:
        """
        Stream chunks. Yields `Result` per chunk (Ok or Err).
        Caller decides what to do with errors mid-stream.

        `idempotency_key` is accepted for Protocol conformance
        (LiteLLMTool does not dedupe; see invoke() docstring).

        Iter 22: ``temperature`` and ``max_tokens`` default
        to ``None`` (sentinel: "use Settings"); same
        resolution as ``invoke()``.
        """
        effective_temperature, effective_max_tokens = self._effective_kwargs(
            temperature, max_tokens
        )

        # The previous implementation used `asyncio.wait_for`
        # over the async generator, but `wait_for` requires
        # an awaitable, not an iterator. Instead, the
        # timeout is applied per-chunk: if the next chunk
        # takes longer than `timeout_s`, we abort. This
        # approximates the original intent (catch hung
        # streams) without misusing `wait_for`.
        import time as _time

        gen = _astream_litellm_inner(
            model=model or self._default_model,
            system=system,
            user=user,
            temperature=effective_temperature,
            max_tokens=effective_max_tokens,
            **kwargs,
        )
        deadline = _time.monotonic() + self._timeout_s
        try:
            while True:
                chunk = await self._next_chunk_with_timeout(gen, deadline)
                if chunk is _STREAM_DONE:
                    return
                if chunk is _STREAM_TIMEOUT:
                    yield Err(ToolError(f"timeout after {self._timeout_s}s"))
                    return
                yield chunk
        except Exception as e:
            yield Err(ToolError(f"llm_error: {e!r}"))

    @staticmethod
    async def _next_chunk_with_timeout(
        gen: AsyncIterator[Result[LLMChunk, ToolError]],
        deadline_monotonic: float,
    ) -> Any:
        """
        Pull the next chunk from `gen`, enforcing the
        absolute deadline `deadline_monotonic`. Returns:

          - the next chunk (`Result[LLMChunk, ToolError]`)
            on success.
          - `_STREAM_DONE` on `StopAsyncIteration`.
          - `_STREAM_TIMEOUT` if the chunk does not arrive
            before the deadline.

        The deadline is a single timestamp (not a
        duration) so successive calls share a single
        budget — the stream is not given a fresh
        `timeout_s` per chunk.
        """
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return _STREAM_TIMEOUT
        try:
            return await asyncio.wait_for(gen.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return _STREAM_DONE
        except asyncio.TimeoutError:
            return _STREAM_TIMEOUT

    # ------------------------------------------------------------------ internal

    async def _call_litellm(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        response_format: Optional[dict],
        idempotency_key: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        One call to the transport. Async, returns LLMResponse.
        Raises on any transport error (callers handle
        rate-limit / auth / timeout distinction).

        Resilience layer (circuit breaker / retry) is
        delegated to `_execute_with_resilience`. When no
        resilience is configured, the call goes straight
        to the transport.
        """
        transport = self._get_transport()
        # Iter 28 FU 3: ``LLMTransport`` is now a
        # ``Callable[LLMRequest, dict]``. The 9
        # keyword parameters of the old ``complete()``
        # are bundled into an ``LLMRequest``.
        request = LLMRequest(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            drop_unsupported_params=self._drop,
            idempotency_key=idempotency_key,
            extra=kwargs,
        )

        started = time.perf_counter()
        completion = await self._execute_with_resilience(lambda: transport(request))
        latency_ms = (time.perf_counter() - started) * 1000.0
        return _to_llm_response(completion, model, latency_ms)

    async def _execute_with_resilience(
        self, do_call: Callable[[], Awaitable[Any]]
    ) -> Any:
        """
        Apply the configured resilience layer around
        `do_call()` and return its result. Raises
        `BusinessError` (from the circuit breaker) so
        the caller can branch on it.

        Strategy is selected by configuration:

          1. **Circuit breaker** (when set) wins over
             retry — the breaker counts every call as a
             discrete failure unit; stacking a retry
             inside the breaker would double-count.
          2. **Retry** (when `retry_attempts >= 1`)
             wraps the call in `with_timeout_and_retry`
             for the "call hung longer than expected"
             case. We retry on `TimeoutError` only;
             transport-level errors are delegated to the
             fallback loop in `invoke()`.
          3. **Direct** (default) — single call.
        """
        if self._circuit_breaker is not None:
            # The breaker's `call` returns
            # `Result[Any, BusinessError]`. On
            # rejection (OPEN or inner failure) we
            # re-raise the BusinessError so the
            # fallback loop in `invoke()` can switch
            # to the next model in `fallback_models`.
            breaker_result = await self._circuit_breaker.call(do_call)
            if breaker_result.is_err():
                raise breaker_result.err_value()
            return breaker_result.ok_value()
        if self._retry_attempts >= 1:
            return await with_timeout_and_retry(
                do_call,
                timeout_seconds=self._timeout_s,
                backoff=BackoffPolicy(
                    max_attempts=self._retry_attempts + 1,
                    base_delay=self._retry_base_delay,
                    max_delay=self._retry_max_delay,
                    max_total_seconds=self._retry_max_total_seconds,
                    retry_on=(asyncio.TimeoutError,),
                ),
            )
        return await do_call()

    def _get_transport(self) -> "LLMTransport":
        if self._transport is None:
            self._transport = LiteLLMTransportAdapter()
        return self._transport


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
        completion_kwargs: dict = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_format is not None:
            litellm_params = request.extra.get("litellm_params", {})
            litellm_params["response_format"] = request.response_format
            request.extra = {
                **request.extra,
                "litellm_params": litellm_params,
            }
        completion_kwargs.update(request.extra)
        # The runtime type is `ModelResponse | CustomStreamWrapper`.
        # Stream wrappers are iterable (not full responses),
        # but in this code path `stream` is False (we use
        # `acompletion`, not `astream`), so the runtime
        # type is always `ModelResponse` (a pydantic model).
        # We annotate as `Any` and probe carefully.
        try:
            response: Any = await litellm.acompletion(**completion_kwargs)
        except litellm.RateLimitError as e:
            # Provider 429. Translate to typed LLM
            # exception so the fallback loop can switch
            # to the next model in `fallback_models`.
            raise LLMRateLimitError(str(e)) from e
        except litellm.AuthenticationError as e:
            # Provider 401/403. Non-recoverable.
            raise LLMAuthError(str(e)) from e
        except litellm.APIError as e:
            # Generic provider error. Surface as a
            # typed `LLMError`; the fallback loop
            # treats this as non-recoverable.
            raise LLMError(str(e)) from e
        # pydantic v2 ModelResponse has model_dump().
        # The probe via `is not None` after getattr is a
        # way to satisfy pyright without `type: ignore`.
        dump = getattr(response, "model_dump", None)
        if callable(dump):
            try:
                return response.model_dump()
            except Exception:
                pass
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
        except Exception:
            pass
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


def configure_litellm_env() -> None:
    """
    Helper: turn on `drop_params` globally and disable
    telemetry. Call once at process start. Not required —
    LiteLLMTool sets `drop_params` per-call.
    """
    import litellm

    litellm.drop_params = True
    os.environ.setdefault("LITELLM_TELEMETRY", "False")
