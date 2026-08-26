# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``tools/registry.py`` (``ToolRegistry``).

Closes the tools/registry coverage gap (DEBT §3, 89% → 100%).
The ``list_descriptors`` method is covered by
``test_list_descriptors.py`` (schema round-trip + the
unserialisable branch); the new module covers the rest
of the public surface:

  - ``__init__`` + the two internal dicts.
  - ``register`` (default ACL + custom ACL) and
    ``register_with_acl`` (the convenience wrapper).
  - ``register`` rejecting a duplicate name with
    ``ValueError``.
  - ``set_acl`` (replaces the ACL; raises ``KeyError``
    for unknown tools).
  - ``acl_for`` (the framework's single read path).
  - ``unregister`` (silently no-op for unknown tools).
  - ``get`` / ``names`` / ``tools`` /
    ``__contains__`` / ``__len__`` — the introspection
    API used by the dispatcher and the API gateway.
  - ``_schema_to_json`` — the round-trip + the three
    failure modes (non-serialisable,
    ``<...object at 0x...>`` repr, post-roundtrip
    parse failure).
"""

from __future__ import annotations

import json
from typing import Optional

import pytest

from kntgraph.core.result import Ok
from kntgraph.tools.acl import ToolACL, default_acl
from kntgraph.tools.registry import ToolRegistry

# ADR-066 §4.4 (v0.17): ``ToolRegistry.__init__``
# emits a ``DeprecationWarning``. The fixture
# constructs one for every test; the existing
# tests cover behaviour, not the deprecation
# signal. Silence the warning here so the
# existing tests stay readable; the
# ``TestDeprecation`` class below is the single
# explicit assertion of the warning.
pytestmark = pytest.mark.filterwarnings(
    "ignore:ToolRegistry is deprecated:DeprecationWarning"
)


# ---------------------------------------------------------------------------
# Test fixtures: minimal Tool classes (duck-typed; the registry
# only reads ``name`` / ``description`` / ``input_schema``).
# ---------------------------------------------------------------------------


class _EchoTool:
    name = "echo"
    description = "Echoes the input."
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def invoke(self, *, idempotency_key: str, **kwargs: object) -> Ok:
        return Ok({"text": kwargs.get("text", "")})


class _OtherTool:
    name = "other"
    description = "Other tool."
    input_schema = None

    async def invoke(self, *, idempotency_key: str, **kwargs: object) -> Ok:
        return Ok({})


class _NoSchemaTool:
    name = "no_schema"
    description = "No schema."
    input_schema = None

    async def invoke(self, *, idempotency_key: str, **kwargs: object) -> Ok:
        return Ok({})


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


# ---------------------------------------------------------------------------
# __init__ + internal state
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_empty_state(self, registry: ToolRegistry) -> None:
        assert registry.names() == []
        assert registry.tools() == []
        assert len(registry) == 0
        assert "echo" not in registry


# ---------------------------------------------------------------------------
# register / register_with_acl
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_assigns_default_acl(self, registry: ToolRegistry) -> None:
        registry.register(_EchoTool())
        assert registry.acl_for("echo") == default_acl()

    def test_register_with_custom_acl(self, registry: ToolRegistry) -> None:
        acl = ToolACL(
            required_role="admin",
            tenant_pinned=True,
            tenant_id="tenant-1",
        )
        registry.register(_EchoTool(), acl=acl)
        assert registry.acl_for("echo") == acl
        assert registry.acl_for("echo").required_role == "admin"

    def test_register_with_acl_convenience(self, registry: ToolRegistry) -> None:
        acl = ToolACL(
            required_role="supervisor",
            tenant_pinned=False,
        )
        registry.register_with_acl(_EchoTool(), acl=acl)
        assert registry.acl_for("echo") == acl
        assert registry.acl_for("echo").required_role == "supervisor"

    def test_register_duplicate_raises(self, registry: ToolRegistry) -> None:
        registry.register(_EchoTool())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_EchoTool())

    def test_register_replaces_acl_on_duplicate(self, registry: ToolRegistry) -> None:
        # The ValueError is raised BEFORE the ACL is
        # replaced — the second call does NOT mutate
        # the registry's internal state.
        registry.register(_EchoTool())
        acl = ToolACL(
            required_role="admin",
            tenant_pinned=True,
            tenant_id="tenant-1",
        )
        with pytest.raises(ValueError):
            registry.register(_EchoTool(), acl=acl)
        assert registry.acl_for("echo") == default_acl()


# ---------------------------------------------------------------------------
# set_acl
# ---------------------------------------------------------------------------


class TestSetAcl:
    def test_set_acl_replaces_existing(self, registry: ToolRegistry) -> None:
        registry.register(_EchoTool())
        acl = ToolACL(
            required_role="admin",
            tenant_pinned=True,
            tenant_id="tenant-1",
        )
        registry.set_acl("echo", acl)
        assert registry.acl_for("echo") == acl

    def test_set_acl_raises_for_unknown_tool(self, registry: ToolRegistry) -> None:
        acl = ToolACL(
            required_role="admin",
            tenant_pinned=True,
            tenant_id="tenant-1",
        )
        with pytest.raises(KeyError, match="not registered"):
            registry.set_acl("nonexistent", acl)


# ---------------------------------------------------------------------------
# acl_for
# ---------------------------------------------------------------------------


class TestAclFor:
    def test_acl_for_unknown_tool_returns_none(self, registry: ToolRegistry) -> None:
        assert registry.acl_for("nonexistent") is None

    def test_acl_for_registered_tool_returns_default(
        self, registry: ToolRegistry
    ) -> None:
        registry.register(_EchoTool())
        acl: Optional[ToolACL] = registry.acl_for("echo")
        assert acl is not None
        assert acl.required_role == "agent"
        assert acl.tenant_pinned is False


# ---------------------------------------------------------------------------
# unregister
# ---------------------------------------------------------------------------


class TestUnregister:
    def test_unregister_removes_tool_and_acl(self, registry: ToolRegistry) -> None:
        registry.register(_EchoTool())
        registry.unregister("echo")
        assert registry.get("echo") is None
        assert registry.acl_for("echo") is None
        assert "echo" not in registry

    def test_unregister_unknown_is_noop(self, registry: ToolRegistry) -> None:
        registry.unregister("nonexistent")
        assert registry.names() == []


# ---------------------------------------------------------------------------
# get / names / tools / __contains__ / __len__
# ---------------------------------------------------------------------------


class TestIntrospection:
    def test_get_returns_tool(self, registry: ToolRegistry) -> None:
        tool = _EchoTool()
        registry.register(tool)
        assert registry.get("echo") is tool

    def test_get_unknown_returns_none(self, registry: ToolRegistry) -> None:
        assert registry.get("nonexistent") is None

    def test_names_returns_sorted_insertion_order(self, registry: ToolRegistry) -> None:
        registry.register(_EchoTool())
        registry.register(_OtherTool())
        assert registry.names() == ["echo", "other"]

    def test_tools_returns_all_tools(self, registry: ToolRegistry) -> None:
        echo = _EchoTool()
        other = _OtherTool()
        registry.register(echo)
        registry.register(other)
        tools = registry.tools()
        assert len(tools) == 2
        assert echo in tools
        assert other in tools

    def test_contains(self, registry: ToolRegistry) -> None:
        registry.register(_EchoTool())
        assert "echo" in registry
        assert "nonexistent" not in registry

    def test_len(self, registry: ToolRegistry) -> None:
        assert len(registry) == 0
        registry.register(_EchoTool())
        assert len(registry) == 1
        registry.register(_OtherTool())
        assert len(registry) == 2


# ---------------------------------------------------------------------------
# _schema_to_json
# ---------------------------------------------------------------------------


class TestSchemaToJson:
    def test_none_schema_returns_empty_dict(self) -> None:
        from kntgraph.tools.registry import _schema_to_json

        assert _schema_to_json(None) == "{}"

    def test_dict_schema_round_trips(self) -> None:
        from kntgraph.tools.registry import _schema_to_json

        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        result = _schema_to_json(schema)
        assert result is not None
        assert json.loads(result) == schema

    def test_non_serialisable_schema_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kntgraph.tools.registry import _schema_to_json

        class _NotJSON:
            def __repr__(self) -> str:
                raise TypeError("no repr")

        result = _schema_to_json(_NotJSON())
        assert result is None

    def test_object_repr_in_serialised_string_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An object whose ``__repr__`` returns the
        # ``<...object at 0x...>`` pattern passes
        # ``json.dumps`` (via ``default=str``) but the
        # registry rejects the post-roundtrip result.
        from kntgraph.tools.registry import _schema_to_json

        class _Weird:
            def __repr__(self) -> str:
                return "<Weird object at 0x7f9c0>"

        result = _schema_to_json(_Weird())
        assert result is None

    def test_dumps_succeeds_but_loads_fails_returns_none(
        self,
    ) -> None:
        # The post-roundtrip ``json.loads`` check is
        # defensive: ``json.dumps(default=str)`` accepts
        # any object, so the round-trip should always
        # succeed. The only way to reach the
        # ``except (TypeError, ValueError)`` arm at
        # lines 175-181 is to feed a payload that
        # ``dumps`` accepts but ``loads`` rejects — e.g.
        # a string that is not valid JSON. We construct
        # this by monkey-patching ``json.dumps`` to
        # return such a payload; the registry's
        # ``_schema_to_json`` does not pre-validate.
        from kntgraph.tools import registry as reg_mod

        original_dumps = reg_mod.json.dumps
        reg_mod.json.dumps = lambda *a, **k: "not-a-json-payload"  # type: ignore[assignment]
        try:
            result = reg_mod._schema_to_json({"k": "v"})
            assert result is None
        finally:
            reg_mod.json.dumps = original_dumps  # type: ignore[assignment]


class TestDeprecation:
    """ADR-066 §4.4 (v0.17): ``ToolRegistry()``
    emits a ``DeprecationWarning`` pointing to
    ``WorkerManager.register(tool_cls, acl=...)``
    as the replacement. The module-level
    ``pytestmark = filterwarnings("ignore:...")``
    silences the warning for the rest of this
    file; this class opts back in to validate
    the signal is emitted.
    """

    def test_init_emits_deprecation_warning(self) -> None:
        """Constructing a ``ToolRegistry`` emits a
        ``DeprecationWarning`` mentioning
        ``WorkerManager`` as the replacement."""
        with pytest.warns(DeprecationWarning, match="WorkerManager"):
            ToolRegistry()
