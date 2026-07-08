# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the @tool_worker decorator and Worker metadata extraction.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from kntgraph.core.result import Result, Ok
from kntgraph.tools.protocol import Describable

# We will implement this in kntgraph/tools/worker.py
from kntgraph.tools.worker import tool_worker


def test_tool_worker_decorator_adds_describable_metadata():
    """
    @tool_worker must inject name, description, and input_schema
    into the class so it satisfies the Describable protocol.
    """

    @tool_worker(
        name="test_tool",
        description="A tool for testing.",
        max_concurrency=5,
        retries=2,
    )
    class MyTool:
        async def invoke(
            self, *, idempotency_key: str, user_id: str, age: int
        ) -> Result[dict, str]:
            return Ok({"status": "ok"})

    # The class itself should now have the metadata
    assert MyTool.name == "test_tool"
    assert MyTool.description == "A tool for testing."

    # It must have max_concurrency and retries as well
    assert MyTool.__tool_worker_max_concurrency__ == 5
    assert MyTool.__tool_worker_retries__ == 2

    # The schema should be inferred from the invoke signature (excluding self and idempotency_key)
    schema = MyTool.input_schema
    assert schema["type"] == "object"
    assert "user_id" in schema["properties"]
    assert schema["properties"]["user_id"]["type"] == "string"
    assert "age" in schema["properties"]
    assert schema["properties"]["age"]["type"] == "integer"
    assert "user_id" in schema["required"]
    assert "age" in schema["required"]

    # Instances should satisfy Describable
    instance = MyTool()
    assert isinstance(instance, Describable)


class UserPayload(BaseModel):
    email: str
    is_active: bool


@tool_worker(name="complex_tool", description="Complex tool.")
class ComplexTool:
    async def invoke(
        self, *, idempotency_key: str, payload: UserPayload
    ) -> Result[bool, str]:
        return Ok(True)


def test_tool_worker_with_pydantic_model():
    """
    If the signature uses Pydantic models, the schema should be extracted correctly.
    """
    schema = ComplexTool.input_schema
    assert "payload" in schema["properties"]
    payload_prop = schema["properties"]["payload"]
    assert "$ref" in payload_prop
    assert payload_prop["$ref"] == "#/$defs/UserPayload"

    assert "$defs" in schema
    assert "UserPayload" in schema["$defs"]
    user_payload_def = schema["$defs"]["UserPayload"]
    assert user_payload_def["type"] == "object"
    assert "email" in user_payload_def["properties"]
    assert "is_active" in user_payload_def["properties"]


def test_tool_worker_missing_invoke_method():
    """
    If the class doesn't have an invoke method, the decorator should raise an error early.
    """
    with pytest.raises(TypeError, match="must implement an 'invoke' method"):

        @tool_worker(name="bad_tool", description="...")
        class BadTool:
            pass


def test_tool_worker_missing_idempotency_key():
    """
    If the invoke method doesn't accept idempotency_key, it should raise an error.
    """
    with pytest.raises(
        TypeError, match="must accept 'idempotency_key' as a keyword-only argument"
    ):

        @tool_worker(name="bad_tool", description="...")
        class BadTool:
            async def invoke(self, *, user_id: str) -> Result[str, str]:
                return Ok("ok")
