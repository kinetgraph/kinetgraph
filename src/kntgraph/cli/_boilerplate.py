# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.cli._boilerplate -- which files the CLI owns.

The ``knt new <artifact>`` commands render Jinja templates
into ``src/<package>/contexts/<context>/...`` files. When the
framework evolves, the boilerplate on disk goes stale: the user
has to either ``git rm`` the old file and re-run ``knt new``
(losing local edits) or hand-merge the diff (error-prone).

This module is the **knowledge** of that workflow: which files
are boilerplate, where they live, and what each template needs
to render them. The ``knt upgrade`` command acts on it.

The discovery of "which files are boilerplate" is **path-based**:
files under ``src/<package>/contexts/<context>/{agents,
components,events,systems,tools}/`` plus the synthesised root
files (``<context>/dispatcher.py``, ``<project>/consumer.py``,
``<project>/core/config.py``, ``<project>/main.py``).

Two of the templates are **derived** from the bounded context
(ADR-088): the agent declaration and the dispatcher. Their
contexts come from ``kntgraph.cli._derive``.

The CLI does NOT touch user-modified application code. The
template renderer is purely substitutional and never inspects
the user's logical changes -- it only knows the template
contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from kntgraph.cli._derive import (
    AGENT_TEMPLATE,
    DISPATCHER_TEMPLATE,
    resolve_agent_context,
    resolve_dispatcher_context,
)
from kntgraph.cli._project import snake_to_camel
from kntgraph.cli._scanner import (
    discover_artifacts,
    discover_context_names,
    discover_package_name,
    has_supervisor_artifact,
)
from kntgraph.core._typing import JsonValue

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


# The **canonical** mapping of (template_name, rendered_path,
# default_context). ``rendered_path`` is relative to the project
# root. ``default_context`` is a callable that takes the
# package_name + context_name + opts and returns the context
# dict the jinja template expects.
#
# This is the **only** place the upgrade machinery knows about
# boilerplate locations. Adding a new template means adding one
# entry here; the ``knt upgrade`` CLI picks it up automatically.
@dataclass(frozen=True)
class _BoilerplateMapping:
    template_name: str
    rendered_path: str  # relative to project root
    requires_package: bool = True
    requires_context: bool = False
    # The HTTP-gateway scaffold owns these two files
    # (``knt init project --use-intent-http``). A project
    # without the gateway never had them, so they are not
    # boilerplate there -- listing them unconditionally made
    # ``knt upgrade check`` report a phantom drift.
    requires_http: bool = False


# Path templates use ``{package}`` and ``{context}`` placeholders.
# The rendered path uses the same placeholders.
_MAPPING: tuple[_BoilerplateMapping, ...] = (
    # Root-level files (no context).
    _BoilerplateMapping(
        template_name="main.py.jinja",
        rendered_path="src/{package}/main.py",
        requires_package=True,
        requires_context=False,
    ),
    _BoilerplateMapping(
        template_name="config.py.jinja",
        rendered_path="src/{package}/core/config.py",
        requires_package=True,
        requires_context=False,
        requires_http=True,
    ),
    _BoilerplateMapping(
        template_name="consumer.py.jinja",
        rendered_path="src/{package}/consumer.py",
        requires_package=True,
        requires_context=False,
        requires_http=True,
    ),
    # Per-context files.
    _BoilerplateMapping(
        template_name="dispatcher.py.jinja",
        rendered_path="src/{package}/contexts/{context}/dispatcher.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="agent.py.jinja",
        rendered_path="src/{package}/contexts/{context}/agents/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="event.py.jinja",
        rendered_path="src/{package}/contexts/{context}/events/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="system.py.jinja",
        rendered_path="src/{package}/contexts/{context}/systems/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="tool.py.jinja",
        rendered_path="src/{package}/contexts/{context}/tools/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
    _BoilerplateMapping(
        template_name="component.py.jinja",
        rendered_path="src/{package}/contexts/{context}/components/<name>.py",
        requires_package=True,
        requires_context=True,
    ),
)


def _base_context(package: str, project_name: str) -> dict[str, JsonValue]:
    """The minimum context shared by every template."""
    return {
        "project_name": project_name,
        "package": package,
    }


def render_boilerplate(
    template_name: str,
    context: dict[str, JsonValue],
) -> str:
    """Render a template by name with the given context."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=False,  # nosec B701
    )
    tmpl = env.get_template(template_name)
    return tmpl.render(context)


def resolve_mappings() -> list[tuple[Path, str, dict[str, JsonValue]]]:
    """Resolve every boilerplate mapping to a concrete (path, template, ctx)
    triple. Returns the list of files that ``knt upgrade`` can act on.

    The function is a thin orchestrator: it gathers the
    package-level context (project flags, context names)
    and dispatches per-mapping to a small helper. Each
    helper focuses on one mapping shape (project-level /
    per-context / per-artifact). The CC stays below 10
    because the branching lives in the per-shape helpers
    (AGENTS.md §3.2 + DEBT §2.26 ``CC offenders``).
    """
    package = discover_package_name()
    if package is None:
        return []
    project_name = package  # convention: project name == package name

    init_flags = _infer_init_flags(package)
    context_names = discover_context_names()

    resolved: list[tuple[Path, str, dict[str, JsonValue]]] = []
    for mapping in _MAPPING:
        if not mapping.requires_package:
            continue
        resolved.extend(
            _resolve_mapping(
                mapping=mapping,
                package=package,
                project_name=project_name,
                init_flags=init_flags,
                context_names=context_names,
            )
        )
    return resolved


def _infer_init_flags(
    package: str,
) -> dict[str, JsonValue]:
    """Infer the ``init.py`` flags from the generated
    ``main.py``.

    The project-level templates (``main.py.jinja``,
    ``consumer.py.jinja``) require ``use_intent_http``
    and ``routing_mode``. The values are recovered
    heuristically from the existing ``main.py``:

    - ``use_intent_http``: True when the file imports
      ``from kntgraph.api import create_app`` (the HTTP
      path imports it; the routing-only path does not).
    - ``routing_mode``: parsed from the
      ``routing.adapters.<mode> import`` line. Defaults
      to ``"external"`` when the snippet is absent
      (e.g. the HTTP path does not import the adapter).
    """
    main_path = Path("src") / package / "main.py"
    use_intent_http = False
    routing_mode = "external"
    if not main_path.exists():
        return {
            "use_intent_http": use_intent_http,
            "routing_mode": routing_mode,
        }
    text = main_path.read_text(encoding="utf-8")
    use_intent_http = "from kntgraph.api import create_app" in text
    m = re.search(r"routing\.adapters\.(\w+)\s+import", text)
    if m:
        routing_mode = m.group(1)
    return {
        "use_intent_http": use_intent_http,
        "routing_mode": routing_mode,
    }


def _resolve_mapping(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    project_name: str,
    init_flags: dict[str, JsonValue],
    context_names: list[str],
) -> list[tuple[Path, str, dict[str, JsonValue]]]:
    """Dispatch one mapping to the right per-shape helper.

    The shape is determined by two flags on the mapping:

    - ``requires_context``: per-context (one file per
      context, e.g. ``dispatcher.py``) or per-artifact
      (one file per artifact within a context, e.g.
      ``agents``, ``events``).
    - ``requires_package`` and ``template_name``:
      project-level files (e.g. ``main.py``,
      ``config.py``, ``consumer.py``) share the
      ``init_flags`` context.
    """
    if mapping.requires_context:
        return _resolve_context_mapping(
            mapping=mapping,
            package=package,
            context_names=context_names,
            base_ctx=_base_context(package, project_name),
        )
    if _skipped_without_http(mapping, init_flags):
        return []
    return _resolve_project_mapping(
        mapping=mapping,
        package=package,
        base_ctx=_base_context(package, project_name),
        init_flags=init_flags,
    )


def _skipped_without_http(
    mapping: _BoilerplateMapping,
    init_flags: dict[str, JsonValue],
) -> bool:
    """Whether the mapping belongs to a project without the gateway.

    The HTTP-gateway scaffold owns ``consumer.py`` and
    ``core/config.py``. A project the gateway was never
    scaffolded into never had them, so they are not boilerplate
    there -- listing them unconditionally made ``knt upgrade
    check`` report a phantom drift.
    """
    if not mapping.requires_http:
        return False
    return not bool(init_flags.get("use_intent_http"))


def _resolve_project_mapping(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    base_ctx: dict[str, JsonValue],
    init_flags: dict[str, JsonValue],
) -> list[tuple[Path, str, dict[str, JsonValue]]]:
    """Project-level boilerplate (``main.py``,
    ``consumer.py``, ``config.py``). The ``main.py.jinja``
    and ``consumer.py.jinja`` templates consume the
    ``init_flags``; ``config.py.jinja`` does not.
    """
    ctx = base_ctx
    if mapping.template_name in ("main.py.jinja", "consumer.py.jinja"):
        ctx = {**ctx, **init_flags}
    return [
        (
            Path(mapping.rendered_path.format(package=package)),
            mapping.template_name,
            ctx,
        ),
    ]


def _resolve_context_mapping(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    context_names: list[str],
    base_ctx: dict[str, JsonValue],
) -> list[tuple[Path, str, dict[str, JsonValue]]]:
    """Per-context boilerplate. For per-artifact templates
    (``agent.py.jinja``, ``event.py.jinja``, etc.) we
    emit one entry per existing artifact; for singleton
    templates (``dispatcher.py.jinja``) we emit one entry
    per context.
    """
    resolved: list[tuple[Path, str, dict[str, JsonValue]]] = []
    kind = _template_to_kind(mapping.template_name)
    for context_name in context_names:
        ctx_with_c: dict[str, JsonValue] = {**base_ctx, "context_name": context_name}
        if kind is None:
            resolved.append(
                _resolve_context_singleton(
                    mapping=mapping,
                    package=package,
                    context_name=context_name,
                    ctx_with_c=ctx_with_c,
                ),
            )
            continue
        resolved.extend(
            _resolve_context_artifacts(
                mapping=mapping,
                package=package,
                context_name=context_name,
                kind=kind,
                ctx_with_c=ctx_with_c,
            )
        )
    return resolved


def _resolve_context_singleton(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    context_name: str,
    ctx_with_c: dict[str, JsonValue],
) -> tuple[Path, str, dict[str, JsonValue]]:
    """One entry per context (e.g. ``dispatcher.py``)."""
    if mapping.template_name == DISPATCHER_TEMPLATE:
        return (
            Path(
                mapping.rendered_path.format(
                    package=package,
                    context=context_name,
                ),
            ),
            mapping.template_name,
            resolve_dispatcher_context(package, context_name),
        )
    return (
        Path(
            mapping.rendered_path.format(
                package=package,
                context=context_name,
            ),
        ),
        mapping.template_name,
        {
            **ctx_with_c,
            "with_supervisor": has_supervisor_artifact(context_name),
        },
    )


def _resolve_context_artifacts(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    context_name: str,
    kind: str,
    ctx_with_c: dict[str, JsonValue],
) -> list[tuple[Path, str, dict[str, JsonValue]]]:
    """One entry per existing artifact inside the context
    (e.g. every ``agents/<name>.py``, every
    ``events/<name>.py``). The ``<name>`` placeholder in
    the mapping's ``rendered_path`` is substituted with
    the artifact's filename stem.
    """
    resolved: list[tuple[Path, str, dict[str, JsonValue]]] = []
    for artifact in discover_artifacts(context_name).get(kind, []):
        artifact_ctx = _artifact_context(
            mapping=mapping,
            package=package,
            context_name=context_name,
            artifact=artifact,
            ctx_with_c=ctx_with_c,
        )
        resolved.append(
            (
                Path(
                    mapping.rendered_path.format(
                        package=package,
                        context=context_name,
                    ).replace("<name>", artifact),
                ),
                mapping.template_name,
                artifact_ctx,
            ),
        )
    return resolved


def _artifact_context(
    *,
    mapping: _BoilerplateMapping,
    package: str,
    context_name: str,
    artifact: str,
    ctx_with_c: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """The template context of one artifact.

    The ``agent.py.jinja`` expects ``camel_case_name`` (the
    original CamelCase input). The on-disk name is
    ``agent_name`` (snake_case). The discoverer doesn't know
    the original CamelCase form -- we synthesise it from the
    filename.

    The agent declaration is *derived*: its allow-list, its
    domain types and its two execution cycles come from the
    context's other artifacts.
    """
    if mapping.template_name == AGENT_TEMPLATE:
        return resolve_agent_context(package, context_name, artifact)
    return {
        **ctx_with_c,
        "agent_name": artifact,
        "system_name": artifact,
        "tool_name": artifact,
        "event_name": artifact,
        "camel_case_name": snake_to_camel(artifact),
        "event_type": f"{context_name}.{artifact}",
        "with_supervisor": has_supervisor_artifact(context_name),
    }


def _template_to_kind(template_name: str) -> str | None:
    """Map a template name to its artifact kind (``agents``,
    ``events``, ``systems``, ``tools``, ``components``). Returns
    ``None`` for templates that are not per-artifact.
    """
    return {
        "agent.py.jinja": "agents",
        "event.py.jinja": "events",
        "system.py.jinja": "systems",
        "tool.py.jinja": "tools",
        "component.py.jinja": "components",
    }.get(template_name)


__all__ = [
    "render_boilerplate",
    "resolve_mappings",
]
