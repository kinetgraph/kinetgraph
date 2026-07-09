# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
PlannerRole — semantic specialization: produce a Plan from a task.

A `Role` is NOT a Tool. It uses a `LiteLLMTool` injected via
constructor. It knows the domain prompt and the output schema.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from kntgraph.core.result import Err, Result, ToolError

from ..tools.llm import LiteLLMTool
from ._base import _BaseLLMRole


# -----------------------------------------------------------------------------
# Output schema
# -----------------------------------------------------------------------------


class PlanStep(BaseModel):
    """One step in a plan."""

    name: str = Field(..., description="Short verb-phrase label")
    description: str = Field(..., description="What this step does")
    depends_on: list[str] = Field(
        default_factory=list,
        description="Names of steps that must run before this one",
    )


class Plan(BaseModel):
    """Typed output of the PlannerRole."""

    goal: str = Field(..., description="Restate the task as a goal")
    steps: list[PlanStep] = Field(..., description="Ordered plan steps")
    rationale: str = Field(..., description="Why this plan makes sense")
    risks: list[str] = Field(
        default_factory=list,
        description="Known risks or open questions",
    )


# -----------------------------------------------------------------------------
# Role
# -----------------------------------------------------------------------------


class PlannerRole(_BaseLLMRole):
    """
    Planner role: task description → Plan.

    Usage:

        llm = LiteLLMTool(default_model="gpt-4o-mini")
        planner = PlannerRole(llm=llm)
        result = await planner.plan(
            "Emitir NF-e de venda para cliente X"
        )
        if result.is_ok():
            plan: Plan = result.unwrap()
            for step in plan.steps:
                print(f"- {step.name}: {step.description}")
    """

    DEFAULT_MAX_TOKENS = 1024
    DEFAULT_TEMPERATURE = 0.0
    OUTPUT_PREFIX = "plan"

    SYSTEM_PROMPT = """\
You are a precise planner. Given a task, produce:
  - `goal`: a one-sentence restatement of the task
  - `steps`: a list of PlanStep, each with:
      - `name`: short verb-phrase (e.g. "validate_cnpj")
      - `description`: what this step does
      - `depends_on`: list of step names that must precede
  - `rationale`: why this ordering makes sense
  - `risks`: list of strings (open questions, things to verify)

Respond ONLY with valid JSON matching this schema:
{
  "goal": str,
  "steps": [{"name": str, "description": str, "depends_on": [str, ...]}],
  "rationale": str,
  "risks": [str, ...]
}

Do not include any prose outside the JSON.
"""

    def __init__(
        self,
        llm: LiteLLMTool,
        *,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        super().__init__(
            llm,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def plan(
        self,
        task: str,
        *,
        context: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        **invoke_kwargs: Any,
    ) -> Result[Plan, ToolError]:
        """
        Produce a Plan for the given task.

        `context` is optional background (e.g. company data,
        prior decisions). The role does NOT inspect it
        semantically — it forwards to the LLM as-is.

        `idempotency_key` defaults to a stable hash of
        (task, context). Same input → same key.

        Extra `**invoke_kwargs` are forwarded to
        `LiteLLMTool.invoke` (e.g. `think=False` for
        thinking Ollama models).
        """
        if (err := self._check_input(task, "task")) is not None:
            return err

        key = idempotency_key or self._stable_key(task, context or "")
        user_parts = [f"Task: {task}"]
        if context:
            user_parts.append(f"\nContext:\n{context}")
        user_parts.append(
            "\nProduce a JSON plan following the schema in the system prompt."
        )
        user_prompt = "\n".join(user_parts)

        r = await self._invoke(
            self.SYSTEM_PROMPT, user_prompt, key=key, **invoke_kwargs
        )
        if r.is_err():
            return Err(r.err_value_or_raise())
        return self._parse_json(r.unwrap().text, Plan, "plan_parse_error")
