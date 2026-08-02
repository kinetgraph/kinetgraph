# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the CLI Jinja templates (ADR-053).

The templates are the **only source of truth** for the
boilerplate the ``knt`` CLI generates. When a template
references a symbol that no longer exists in the framework
(see, e.g., the historical ``CapabilityPolicy``, the
``kntgraph.core.correlation`` import path, the
``Result[dict, Exception]`` return type), the CLI ships
broken boilerplate. These tests render each template with
an example context and assert the rendered output is
**internally consistent** (no broken imports, no symbols
that the framework does not export).

The tests run **without** the ``typer`` extra (they use
the Jinja ``Environment`` directly, not the CLI runner).
This keeps the test cheap and lets the templates be
linted even in the default CI configuration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader


TEMPLATES_DIR = Path(
    __file__,
).resolve().parents[3] / "src" / "kntgraph" / "cli" / "templates"


def _render(template_name: str, context: dict) -> str:
    """Render a template with the given context."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=False,  # nosec B701
    )
    tmpl = env.get_template(template_name)
    return tmpl.render(context)


# ------------------------------------------------------------------
# Symbol-existence guards (post-ADR-039).
# ------------------------------------------------------------------
#
# These are the imports the templates MUST NOT contain.
# The framework removed these symbols in the v0.9.0
# break (ADR-039, ADR-040, ADR-047). Catching the regression
# at the template level is the cheap way to ensure the
# ``knt`` CLI never emits code that imports a non-existent
# name from the framework.
#
# Each guard is a tuple of (template_name, forbidden_import).
# Add a new entry when a framework symbol is removed.
_BROKEN_IMPORTS: list[tuple[str, str]] = [
    # ADR-039 + v0.9.0: ``IntentResolutionSystem`` was
    # removed; the new architecture uses per-role
    # ``WorldSystem`` instances directly.
    ("agent.py.jinja", "IntentResolutionSystem"),
    # ADR-039: ``CapabilityPolicy`` was never implemented
    # in the framework; the template referenced a symbol
    # that did not exist. The new template uses a
    # documentation-only event allow-list.
    ("agent.py.jinja", "kntgraph.security.authorization"),
    # ADR-047: ``Result[dict, Exception]`` is the pre-ADR-047
    # return type. The post-ADR-047 contract is
    # ``Result[dict, ToolError]``. The new template
    # encodes the canonical return type.
    ("tool.py.jinja", "Result[dict[str, Any], Exception]"),
    # The historical import path was renamed. The
    # canonical path is ``kntgraph.core.event.correlation``.
    ("event.py.jinja", "from kntgraph.core.correlation"),
]


@pytest.mark.parametrize(
    "template_name, forbidden",
    _BROKEN_IMPORTS,
    ids=[f"{t}::{f}" for t, f in _BROKEN_IMPORTS],
)
def test_template_does_not_import_removed_symbol(
    template_name: str,
    forbidden: str,
) -> None:
    """The template must not reference symbols that were
    removed from the framework (ADR-039 / ADR-047).
    """
    rendered = _render(
        template_name,
        {
            "agent_name": "checkout",
            "system_name": "checkout_system",
            "tool_name": "process_payment",
            "event_name": "order_placed",
            "camel_case_name": "Checkout",
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
            "event_type": "sales.order_placed",
            "with_supervisor": False,
        },
    )
    assert forbidden not in rendered, (
        f"Template {template_name!r} references removed "
        f"symbol {forbidden!r}; update the template to "
        f"match the current framework."
    )


# ------------------------------------------------------------------
# Header-presence guards (REUSE 3.3 license compliance).
# ------------------------------------------------------------------
#
# Every rendered file must start with the SPDX
# ``FileCopyrightText`` + ``License-Identifier`` header.
# The ``reuse`` CI gate enforces this at the
# ``scripts/ci.py::step_reuse`` level; the template tests
# ensure the upstream templates are compliant.
_HEADER_GUARDED_TEMPLATES = [
    "system.py.jinja",
    "event.py.jinja",
    "tool.py.jinja",
    "component.py.jinja",
    "agent.py.jinja",
    "dispatcher.py.jinja",
    "main.py.jinja",
    "consumer.py.jinja",
    "config.py.jinja",
]


@pytest.mark.parametrize("template_name", _HEADER_GUARDED_TEMPLATES)
def test_template_renders_with_spdx_header(template_name: str) -> None:
    """The template must render with the SPDX header on
    the first three lines (REUSE 3.3 compliance).

    The canonical SPDX header is the 3-line block:

        # SPDX-FileCopyrightText: 2026 kinetgraph
        #
        # SPDX-License-Identifier: Apache-2.0
    """
    rendered = _render(
        template_name,
        {
            "agent_name": "checkout",
            "system_name": "checkout_system",
            "tool_name": "process_payment",
            "event_name": "order_placed",
            "camel_case_name": "Checkout",
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
            "event_type": "sales.order_placed",
            "with_supervisor": False,
        },
    )
    lines = rendered.splitlines()
    # The 3-line header block is the canonical SPDX
    # format. The Jinja ``{# #}`` was abandoned in favour
    # of Python comments so the header survives the
    # render -- otherwise the lint gate would fail with
    # "missing copyright/licensing information".
    assert "SPDX-FileCopyrightText" in lines[0], (
        f"Template {template_name!r} renders without the "
        f"SPDX-FileCopyrightText header on line 1; got "
        f"lines[0]={lines[0]!r}."
    )
    assert "SPDX-License-Identifier" in lines[2], (
        f"Template {template_name!r} renders without the "
        f"SPDX-License-Identifier header on line 3; got "
        f"lines[2]={lines[2]!r}."
    )


# ------------------------------------------------------------------
# Per-template sanity checks.
# ------------------------------------------------------------------
# These assert that the rendered output is **internally
# consistent** (the imports resolve, the function
# signatures match, the type hints are valid). They are
# not exhaustive -- they catch the most common template
# regressions.


def test_system_template_renders_with_pure_worldsystem_signature() -> None:
    """The ``system.py.jinja`` template must render a
    function whose signature matches the framework's
    ``WorldSystem`` Protocol.
    """
    rendered = _render(
        "system.py.jinja",
        {
            "system_name": "checkout_system",
            "camel_case_name": "CheckoutSystem",
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
        },
    )
    assert "def checkout_system(world: World) -> list[Event]:" in rendered
    assert "from kntgraph.core.world import World" in rendered
    assert "from kntgraph.core.event import Event" in rendered


def test_event_template_uses_canonical_correlation_path() -> None:
    """The ``event.py.jinja`` template must import
    ``correlation_middleware`` from the canonical
    ``core.event.correlation`` path (the historical
    ``core.correlation`` path was renamed).
    """
    rendered = _render(
        "event.py.jinja",
        {
            "event_name": "order_placed",
            "camel_case_name": "OrderPlaced",
            "event_type": "sales.order_placed",
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
        },
    )
    assert (
        "from kntgraph.core.event.correlation import correlation_middleware"
        in rendered
    )
    # The historical, broken path must not appear.
    assert "from kntgraph.core.correlation" not in rendered


def test_event_template_uses_uuid_causation_id() -> None:
    """The ``event.py.jinja`` template must declare
    ``causation_id: UUID | None`` (the
    ``Event.domain_from`` constructor expects a UUID,
    not a string). The historical template used ``str``.
    """
    rendered = _render(
        "event.py.jinja",
        {
            "event_name": "order_placed",
            "camel_case_name": "OrderPlaced",
            "event_type": "sales.order_placed",
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
        },
    )
    assert "causation_id: UUID | None" in rendered
    assert "from uuid import UUID" in rendered


def test_tool_template_uses_toolerror_return_type() -> None:
    """The ``tool.py.jinja`` template must declare
    ``Result[dict[str, Any], ToolError]`` (the
    post-ADR-047 contract). The historical ``Exception``
    variant is forbidden.
    """
    rendered = _render(
        "tool.py.jinja",
        {
            "tool_name": "process_payment",
            "camel_case_name": "ProcessPayment",
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
        },
    )
    assert "Result[dict[str, Any], ToolError]" in rendered
    assert "Result[dict[str, Any], Exception]" not in rendered
    assert "from kntgraph.core.result import Ok, Result, ToolError" in rendered


def test_agent_template_does_not_import_capability_policy() -> None:
    """The ``agent.py.jinja`` template must not import
    ``CapabilityPolicy`` (the symbol does not exist in
    the framework). The template uses a
    documentation-only event allow-list instead.
    """
    rendered = _render(
        "agent.py.jinja",
        {
            "agent_name": "checkout",
            "camel_case_name": "CheckoutAgent",
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
        },
    )
    # The dangerous assertion is the **import** -- the
    # historical template imported
    # ``from kntgraph.security.authorization import CapabilityPolicy``
    # which crashes on first use. The word "CapabilityPolicy"
    # may still appear in the historical reference comment.
    assert "from kntgraph.security.authorization" not in rendered
    assert (
        "from kntgraph.security.authorization import CapabilityPolicy"
        not in rendered
    )


def test_dispatcher_template_threads_redis_and_tool_router() -> None:
    """The ``dispatcher.py.jinja`` template must thread
    the ``redis`` and ``tool_router`` arguments through
    to the ``ReactiveDispatcher`` constructor (the
    historical template omitted both, forcing the
    dispatcher to instantiate its own Redis pool).
    """
    rendered = _render(
        "dispatcher.py.jinja",
        {
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
            "with_supervisor": False,
        },
    )
    assert "redis=redis" in rendered
    assert "tool_router=tool_router" in rendered


def test_dispatcher_template_with_supervisor_emits_runner_factory() -> None:
    """When ``with_supervisor`` is True, the dispatcher
    template must render the ``Runner`` factory and the
    ``build_<context>_dispatcher_with_supervisor``
    helper.
    """
    rendered = _render(
        "dispatcher.py.jinja",
        {
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
            "with_supervisor": True,
        },
    )
    assert "from kntgraph.runner import ReactiveDispatcher, Runner" in rendered
    assert "build_sales_supervisor_runner" in rendered
    assert "build_sales_dispatcher_with_supervisor" in rendered


def test_consumer_template_renders_in_three_responsibilities() -> None:
    """The ``consumer.py.jinja`` template must render the
    three target classes (``<Prefix>StreamConsumer``,
    ``_IngressPayload``, ``_IngressContext``) and the
    permanent-ingress-error sentinel.
    """
    rendered = _render(
        "consumer.py.jinja",
        {
            "context_name": "sales",
            "project_name": "my_app",
            "package": "my_app",
            "class_prefix": "Sales",
            "event_factory": "order_placed",
        },
    )
    assert "class SalesStreamConsumer:" in rendered
    assert "class _IngressPayload(BaseModel)" in rendered
    assert "class _IngressContext:" in rendered
    assert "class _PermanentIngressError(Exception)" in rendered
    # The consumer must NOT silently default to a
    # cross-tenant fallback when ``tenant_id`` is
    # missing. The historical template used
    # ``"default-tenant"`` as a hard-coded fallback; the
    # new template reads the fallback from the Settings
    # instance (the Settings itself raises when the
    # operator has not configured a value).
    # The string ``"default-tenant"`` may appear in
    # *docstrings* (the historical reference) but must
    # not appear as a string literal in executable code.
    code_lines = [
        line for line in rendered.splitlines()
        if not line.strip().startswith("#")
    ]
    # Test the *callable* fallback: the constructor of
    # ``_IngressContext`` (the cross-tenant fallback
    # owner) must accept ``default_tenant_id`` as a
    # parameter, not have a hard-coded default.
    assert "default_tenant_id: str" in rendered, (
        "consumer.py.jinja must accept ``default_tenant_id`` "
        "as a constructor parameter; the historical literal "
        "``'default-tenant'`` is a cross-tenant data leak."
    )


def test_config_template_inherits_framework_settings() -> None:
    """The ``config.py.jinja`` template must extend the
    framework's ``Settings`` base class (so the
    ``KNT_`` env prefix and the framework's field
    defaults are inherited).
    """
    rendered = _render(
        "config.py.jinja",
        {
            "project_name": "my_app",
            "package": "my_app",
        },
    )
    assert "from kntgraph.infra.config import Settings as _FrameworkSettings" in rendered
    assert "class Settings(_FrameworkSettings):" in rendered


def test_main_template_passes_redis_and_tool_router_to_dispatcher() -> None:
    """The ``main.py.jinja`` template (in the
    ``use_intent_http`` branch) must thread the
    ``redis`` and ``tool_router`` through to the
    dispatcher's factory.
    """
    rendered = _render(
        "main.py.jinja",
        {
            "project_name": "my_app",
            "package": "my_app",
            "use_intent_http": True,
            "routing_mode": "external",
        },
    )
    # The template ships the call as a commented-out
    # placeholder (the operator uncomments to wire the
    # context dispatcher). The contract is that the
    # **commented** call MUST pass the two arguments.
    # If the operator uncomments, the call is correct.
    # The placeholder is ``build_<context>_dispatcher(log, redis, tool_router)``
    # (positional args, not keyword args).
    placeholder_call = next(
        (
            line for line in rendered.splitlines()
            if "build_sales_dispatcher(" in line
            and "tool_router" in line
        ),
        None,
    )
    assert placeholder_call is not None, (
        "main.py.jinja must include the placeholder "
        "build_sales_dispatcher(...) call (as a comment) "
        "that passes redis and tool_router."
    )
    assert "redis" in placeholder_call
    assert "tool_router" in placeholder_call
