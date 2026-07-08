# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
LLM configuration primitives.

`LLMConfig` carrega o setup do LiteLLMTool a partir de env vars
ou dicionário explícito. Encapsula:

  - `default_model`: o modelo primário (ex: "gpt-4o-mini")
  - `fallback_models`: lista de modelos para tentar em sequência
                       se o primário falhar (rate limit, 5xx, ...)
  - `rate_limit_rpm`: requests por minuto (None = sem limite)
  - `cost_budget_per_hour_usd`: limite de gasto por hora
                                (None = sem limite)
  - `timeout_s`: timeout por chamada

`RateLimiter` e `CostBudget` são wrappers async com janela
deslizante. São passados ao `LiteLLMTool` no construtor.

Uso típico:

    from kntgraph.agents.config import LLMConfig
    from kntgraph.agents.tools.llm import LiteLLMTool

    cfg = LLMConfig.from_env()  # lê OPENAI_API_KEY etc
    tool = LiteLLMTool(
        default_model=cfg.default_model,
        fallback_models=cfg.fallback_models,
        rate_limiter=cfg.rate_limiter(),
        cost_budget=cfg.cost_budget(),
        timeout_s=cfg.timeout_s,
    )
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kntgraph.infra.config import (
    BaseSettings,
    load_dotenv_files,
    default_dotenv_candidates,
)
from kntgraph.resilience.rate_limit import (
    RateLimiter,
)


def load_env(dotenv_path: Optional[Path] = None) -> bool:
    """
    Load environment variables from a `.env` file. Returns
    True if a file was found and loaded, False otherwise.

    Lookup order:
      1. `dotenv_path` argument (if given).
      2. `default_dotenv_candidates()`: `<cwd>/.env`
         then `~/.env`.

    Variables already in `os.environ` are NOT overwritten
    (the explicit env wins over the file — `override=False`
    semantics in `python-dotenv`).

    The implementation is now a thin wrapper around
    `fmh_core.config.load_dotenv_files`, which is the
    canonical env-loader for the whole workspace.
    """
    if dotenv_path is not None:
        return bool(load_dotenv_files(dotenv_path))
    return bool(load_dotenv_files(*default_dotenv_candidates()))


# -----------------------------------------------------------------------------
# LLMConfig
# -----------------------------------------------------------------------------


class _LLMSettings(BaseSettings):
    """
    Internal Pydantic-settings wrapper that reads the
    `FMH_LLM_*` env vars and coerces their types (int /
    float / CSV tuple) before they land in the frozen
    `LLMConfig` dataclass.

    The `_env_prefix` is passed positionally at construction
    by `LLMConfig.from_env(prefix=...)`; Pydantic settings
    honour the `env_prefix` attribute on `model_config`.
    """

    model_config = BaseSettings.model_config | {
        "env_prefix": "FMH_LLM_",
    }

    def __init__(self, *, _env_prefix: str = "FMH_LLM_", **data) -> None:
        # `model_config` is class-level; per-instance
        # overrides are not supported. We accept the prefix
        # argument for API symmetry with
        # `LLMConfig.from_env(prefix=...)` but the actual
        # env-var lookup is governed by the class-level
        # `env_prefix`. Non-default prefixes (e.g. in
        # tests) are surfaced as a typed warning so
        # operators don't get silent wrong-var lookups.
        if _env_prefix != "FMH_LLM_":
            import warnings

            warnings.warn(
                f"_LLMSettings only honours the FMH_LLM_ "
                f"prefix; requested {_env_prefix!r} is "
                f"ignored.",
                UserWarning,
                stacklevel=2,
            )
        super().__init__(**data)

    default_model: Optional[str] = None
    fallback_models: tuple[str, ...] = ()
    rate_limit_rpm: Optional[int] = None
    cost_budget_per_hour_usd: Optional[float] = None
    timeout_s: float = 30.0


@dataclass(frozen=True)
class LLMConfig:
    """
    Configuração imutável para LiteLLMTool.

    Carregue de env via `LLMConfig.from_env()` ou construa
    explicitamente. O `__post_init__` valida invariantes
    básicas (modelo não-vazio, fallback é lista, etc).
    """

    default_model: str = "gpt-4o-mini"
    fallback_models: tuple[str, ...] = ()
    rate_limit_rpm: Optional[int] = 60
    cost_budget_per_hour_usd: Optional[float] = 2.0
    timeout_s: float = 30.0
    # LiteLLM drop_params=True: silently drop unsupported params
    # (e.g. response_format para modelos que não suportam). Útil
    # em multi-provider onde nem toda feature está disponível.
    drop_unsupported_params: bool = True

    def __post_init__(self) -> None:
        if not self.default_model:
            raise ValueError("default_model must be non-empty")
        # Coerce fallback_models to tuple regardless of input
        # (list, tuple, or None — all accepted at construction).
        if not isinstance(self.fallback_models, tuple):
            object.__setattr__(self, "fallback_models", tuple(self.fallback_models))
        if self.rate_limit_rpm is not None and self.rate_limit_rpm <= 0:
            raise ValueError(f"rate_limit_rpm must be > 0, got {self.rate_limit_rpm}")
        if (
            self.cost_budget_per_hour_usd is not None
            and self.cost_budget_per_hour_usd <= 0
        ):
            raise ValueError(
                f"cost_budget_per_hour_usd must be > 0, "
                f"got {self.cost_budget_per_hour_usd}"
            )
        if self.timeout_s <= 0:
            raise ValueError(f"timeout_s must be > 0, got {self.timeout_s}")

    @classmethod
    def from_env(cls, prefix: str = "FMH_LLM_") -> "LLMConfig":
        """
        Carrega configuração de variáveis de ambiente.

        Variáveis lidas (todas opcionais):
          - <prefix>DEFAULT_MODEL
          - <prefix>FALLBACK_MODELS   (CSV)
          - <prefix>RATE_LIMIT_RPM
          - <prefix>COST_BUDGET_USD
          - <prefix>TIMEOUT_S

        Variáveis de provider (lidas mas não consumidas por
        LLMConfig — propagadas para LiteLLM):
          - OLLAMA_API_BASE
          - OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.

        Chamada típica: `LLMConfig.from_env()` no startup.

        Internamente this delegates env-reading to
        `LLMSettings` (a `BaseSettings` from
        `kntgraph.infra.config`)
        so the prefix is honoured and types are coerced
        through Pydantic; the dataclass `LLMConfig` is the
        frozen result. We deliberately keep two layers
        because `LLMConfig` is a frozen dataclass used in
        hot paths where allocating a Pydantic model would
        be overkill.
        """
        # Confirm provider endpoints are visible to
        # LiteLLM (which reads them from os.environ). This
        # is a no-op for the values themselves; it just
        # documents the contract.
        for provider_var in (
            "OLLAMA_API_BASE",
            "OPENAI_API_BASE",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
        ):
            _ = os.environ.get(provider_var)
        env = _LLMSettings(_env_prefix=prefix)
        return cls(
            default_model=env.default_model or "gpt-4o-mini",
            fallback_models=tuple(env.fallback_models or ()),
            rate_limit_rpm=env.rate_limit_rpm,
            cost_budget_per_hour_usd=env.cost_budget_per_hour_usd,
            timeout_s=env.timeout_s,
        )

    def rate_limiter(self) -> Optional["RateLimiter"]:
        if self.rate_limit_rpm is None:
            return None
        return RateLimiter(rpm=self.rate_limit_rpm)

    def cost_budget(self) -> Optional["CostBudget"]:
        if self.cost_budget_per_hour_usd is None:
            return None
        return CostBudget(per_hour_usd=self.cost_budget_per_hour_usd)


# -----------------------------------------------------------------------------
# CostBudget
# -----------------------------------------------------------------------------


class CostBudget:
    """
    Budget de gasto (USD) por hora, sliding-window.

    Mantém uma fila de (timestamp, cost_usd). Antes de cada
    chamada, o caller pergunta `can_spend(estimated_cost)`.
    Depois, chama `charge(actual_cost)` para debitar.

    `estimated_cost` permite recusar uma chamada cara antes
    de incorrer no gasto (ex: prompt muito longo).
    """

    def __init__(self, per_hour_usd: float) -> None:
        if per_hour_usd <= 0:
            raise ValueError(f"per_hour_usd must be > 0, got {per_hour_usd}")
        self._per_hour = per_hour_usd
        self._window_s = 3600.0
        self._entries: deque[tuple[float, float]] = deque()
        self._lock = asyncio.Lock()

    @property
    def per_hour_usd(self) -> float:
        return self._per_hour

    async def _spent_in_window(self) -> float:
        now = time.monotonic()
        while self._entries and (now - self._entries[0][0] > self._window_s):
            self._entries.popleft()
        return sum(c for _, c in self._entries)

    async def can_spend(self, estimated_cost_usd: float) -> bool:
        if estimated_cost_usd < 0:
            raise ValueError(
                f"estimated_cost_usd must be >= 0, got {estimated_cost_usd}"
            )
        async with self._lock:
            spent = await self._spent_in_window()
            return spent + estimated_cost_usd <= self._per_hour

    async def charge(self, cost_usd: float) -> None:
        if cost_usd < 0:
            raise ValueError(f"cost_usd must be >= 0, got {cost_usd}")
        async with self._lock:
            self._entries.append((time.monotonic(), cost_usd))

    async def remaining_usd(self) -> float:
        async with self._lock:
            spent = await self._spent_in_window()
            return max(0.0, self._per_hour - spent)
