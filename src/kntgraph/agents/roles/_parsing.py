# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Parsing helpers for Role outputs.

LLMs (especially local ones) often wrap JSON in markdown
code blocks: ```json\n{...}\n```. Strict JSON parsers fail
on that. `extract_json` finds the first JSON object in the
text and returns it, regardless of fences.

Use `parse_model_json(text, ModelClass)` as the standard
entry point: it tries strict parse, falls back to extraction.
"""

from __future__ import annotations

import json
import re
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> str:
    """
    Extract the first JSON object from `text`.

    Strategy:
      1. Strip markdown fences (``` or ```json).
      2. If the stripped text starts with '{', use it as-is.
      3. Otherwise, find the first '{...}' substring.

    Returns the candidate text (still possibly invalid JSON;
    the caller is expected to json.loads it).
    """
    # 1. Markdown fence
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    # 2. Already a JSON-looking object
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    # 3. Search for the first {...} block
    m = _OBJECT_RE.search(text)
    if m:
        return m.group(0)
    return text


def parse_model_json(text: str, model_cls: Type[T]) -> T:
    """
    Parse `text` as JSON and validate as `model_cls`.

    Tries:
      1. Direct `model_cls.model_validate_json(text)`.
      2. Extract JSON from markdown fences, then validate.
      3. Try to find any JSON object, then validate.

    Raises `ValidationError` if all attempts fail.
    """
    # Attempt 1: strict
    try:
        return model_cls.model_validate_json(text)
    except ValidationError:
        pass

    # Attempt 2: extracted JSON (after fence stripping or
    # {...} search), then json.loads + model_validate.
    candidate = extract_json(text)
    try:
        data = json.loads(candidate)
        return model_cls.model_validate(data)
    except (json.JSONDecodeError, ValidationError):
        pass

    # Attempt 3: last resort — try to parse the original
    # text as JSON, ignoring the validation error context.
    try:
        data = json.loads(text)
        return model_cls.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise e
