# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
concordos._loader -- bundle format loaders (ADR-073 §5).

Three public functions:

  - :func:`load_bundle_dict` -- validate and resolve a dict
    (useful for tests and programmatic construction).
  - :func:`load_bundle_yaml` -- read and parse a YAML file.
  - :func:`load_bundle_json` -- read and parse a JSON file.

Each function returns a :class:`LoadedBundle` containing the
resolved ``FSMConfig`` and ``SagaConfig`` instances ready to
be wrapped in a :class:`ConcordoCatalog` (or used directly).

Errors are reported as :class:`ConcordoBundleError` with a
dotted path (e.g., ``workflow_sagas[0].steps[2].compensate_tool``)
and a hint when applicable. The loader is strict:
unknown keys, unresolved dotted paths, and cross-reference
failures are all rejected.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kntgraph.concordos.schemas import (
    BundleSchema,
    EventSchema,
    FSMConfigSchema,
    SagaConfigSchema,
)

if TYPE_CHECKING:
    from kntgraph.concordos.fsm import FSMConfig
    from kntgraph.concordos.saga import SagaConfig


__all__ = [
    "ConcordoBundleError",
    "LoadedBundle",
    "load_bundle_dict",
    "load_bundle_json",
    "load_bundle_yaml",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConcordoBundleError(ValueError):
    """Raised when a bundle fails validation, cross-reference
    checks, or path resolution.

    Carries a dotted path so the CLI's ``validate`` command
    can print a precise diagnostic.
    """

    def __init__(
        self,
        bundle_id: str | None,
        path: str,
        message: str,
        hint: str | None = None,
    ) -> None:
        self.bundle_id = bundle_id
        self.path = path
        self.message = message
        self.hint = hint

        head = f"[{bundle_id}] " if bundle_id else ""
        body = f"{head}{path}: {message}"
        if hint:
            body += f"\nhint: {hint}"
        super().__init__(body)

    def __str__(self) -> str:
        return self.args[0] if self.args else super().__str__()


# ---------------------------------------------------------------------------
# LoadedBundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedBundle:
    """The result of loading a bundle.

    The loader validates, resolves dotted paths, and
    constructs the Python objects. The returned bundle is
    ready to be wrapped in a :class:`ConcordoCatalog` or
    used directly.

    ``specifications`` carries the raw mini-language
    expressions (per-id) so the caller can build a
    ``SpecRegistry`` later. Resolving expressions to
    ``Specification`` instances is the caller's job
    (typically via :func:`kntgraph.concordos._mini_lang.evaluate`).

    Note: the wire-format bundle (``schemas.BundleSchema``)
    keeps a ``version`` field as user-facing metadata for
    migration signalling (ADR-073). The runtime bundle does
    NOT carry it -- the Concordo Protocol (§3.2) exposes
    only ``(name, systems, projections)``; there is no
    consumer that reads the version off a Concordo instance.
    """

    bundle_id: str
    events: tuple[EventSchema, ...]
    specifications: dict[str, str]  # id -> mini-language expression
    fsm: "FSMConfig | None"
    sagas: tuple["SagaConfig", ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Cross-reference validation
# ---------------------------------------------------------------------------


def _validate_cross_references(bundle: BundleSchema) -> None:
    """Validate that all references in the bundle resolve.

    Checks:
      - transitions[].on_event is in events[].name
      - fsm.on_entry values are in events[].name
      - sagas[].trigger_event is in events[].name
      - sagas[].steps[].compensate_tool is in the worker
        registry (deferred — currently only checks the
        declared events for tool references)
      - specification ids are unique within the bundle
    """
    event_names = {e.name for e in bundle.events}

    def _require_event(path: str, event_name: str) -> None:
        if event_name not in event_names:
            raise ConcordoBundleError(
                bundle_id=bundle.bundle_id,
                path=path,
                message=(
                    f"unknown event {event_name!r}; declared "
                    f"events are: {sorted(event_names) or 'none'}"
                ),
                hint=(
                    f"add an entry under 'events:' for "
                    f"{event_name!r} or correct the typo"
                ),
            )

    # FSM transitions
    if bundle.business_fsm is not None:
        fsm = bundle.business_fsm
        for t in fsm.transitions:
            _require_event(
                f"business_fsm.transitions[{t.from_state!r}].{t.on_event!r}",
                t.on_event,
            )
        for state, event_name in fsm.on_entry.items():
            _require_event(
                f"business_fsm.on_entry[{state!r}]",
                event_name,
            )

    # Saga
    for i, saga in enumerate(bundle.workflow_sagas):
        _require_event(f"workflow_sagas[{i}].trigger_event", saga.trigger_event)
        for j, step in enumerate(saga.steps):
            path_prefix = f"workflow_sagas[{i}].steps[{j}]"
            if step.tool is not None and not _is_valid_identifier(step.tool):
                raise ConcordoBundleError(
                    bundle_id=bundle.bundle_id,
                    path=f"{path_prefix}.tool",
                    message=(
                        f"tool name {step.tool!r} must be a valid "
                        f"Python identifier (the dotted-path form is "
                        f"not yet supported in this version)"
                    ),
                )
            if step.compensate_tool is not None and not _is_valid_identifier(
                step.compensate_tool
            ):
                raise ConcordoBundleError(
                    bundle_id=bundle.bundle_id,
                    path=f"{path_prefix}.compensate_tool",
                    message=(
                        f"compensate_tool {step.compensate_tool!r} "
                        f"must be a valid Python identifier"
                    ),
                )

    # Specification ids are unique
    seen_ids: set[str] = set()
    for spec in bundle.specifications:
        if spec.id in seen_ids:
            raise ConcordoBundleError(
                bundle_id=bundle.bundle_id,
                path=f"specifications[id={spec.id!r}]",
                message=(f"duplicate specification id {spec.id!r}"),
                hint="each specification must have a unique id",
            )
        seen_ids.add(spec.id)


def _is_valid_identifier(s: str) -> bool:
    """A tool name is currently expected to be a plain
    identifier (e.g., ``nfe_emitter``). Dotted-path worker
    registry references are a future extension."""
    return s.isidentifier()


# ---------------------------------------------------------------------------
# Dotted-path resolution
# ---------------------------------------------------------------------------


def _resolve_dotted_path(dotted: str, kind: str, bundle_id: str | None) -> Any:
    """Resolve a dotted Python path (e.g.,
    ``acme.invoice.events.InvoiceSubmitted``) to the
    referenced object via importlib.

    Raises :class:`ConcordoBundleError` on any failure.
    """
    if not dotted or "." not in dotted:
        raise ConcordoBundleError(
            bundle_id=bundle_id,
            path=kind,
            message=(f"{kind!r}={dotted!r} is not a valid dotted path"),
            hint=(
                "expected form like 'package.module.Class' "
                "(e.g., 'acme.invoice.events.InvoiceSubmitted')"
            ),
        )
    try:
        module_path, _, attr = dotted.rpartition(".")
        mod = importlib.import_module(module_path)
        obj = getattr(mod, attr)
    except (ImportError, AttributeError, ValueError) as exc:
        raise ConcordoBundleError(
            bundle_id=bundle_id,
            path=kind,
            message=(
                f"failed to resolve {kind!r}={dotted!r}: {type(exc).__name__}: {exc}"
            ),
            hint=(
                "check the module path and ensure the symbol is "
                "importable from the process running the loader"
            ),
        ) from exc
    return obj


# ---------------------------------------------------------------------------
# Build Python objects from validated schemas
# ---------------------------------------------------------------------------


def _build_fsm_config(fsm: FSMConfigSchema, bundle_id: str | None) -> "FSMConfig":
    """Build a runtime ``FSMConfig`` from a schema.

    The runtime ``FSMConfig`` lives in ``concordos.fsm._config``
    and uses ``DomainComponent`` subclasses. The bundle's
    ``component`` is a dotted path to such a subclass.
    """
    from kntgraph.concordos.fsm import FSMConfig, FSMTransition

    component_class = _resolve_dotted_path(
        fsm.component, "business_fsm.component", bundle_id
    )

    transitions: dict[str, dict[str, FSMTransition]] = {}
    for t in fsm.transitions:
        transitions.setdefault(t.from_state, {})[t.on_event] = FSMTransition(
            to=t.to,
            guard=None,  # guards come from specs
        )

    return FSMConfig(
        component_type=component_class,
        state_field=fsm.state_field,
        transitions=transitions,
        on_entry=dict(fsm.on_entry),
        terminal=frozenset(fsm.terminal),
    )


def _build_saga_config(saga: SagaConfigSchema, bundle_id: str | None) -> "SagaConfig":
    """Build a runtime ``SagaConfig`` from a schema."""
    from kntgraph.concordos.saga import SagaConfig, SagaStepConfig

    steps: list[SagaStepConfig] = []
    for step in saga.steps:
        steps.append(
            SagaStepConfig(
                name=step.name,
                tool_name=step.tool,
                timeout_ms=step.timeout_ms,
                skip_when=None,  # specs are resolved at runtime
                compensate_tool=step.compensate_tool,
                compensate_when=None,
                approval_timeout_ms=step.approval_timeout_ms,
            )
        )

    return SagaConfig(
        name=saga.id.removeprefix("saga:"),
        steps=tuple(steps),
        saga_timeout_ms=saga.saga_timeout_ms,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_bundle_dict(d: dict) -> LoadedBundle:
    """Validate and resolve a bundle from a dict.

    Raises :class:`ConcordoBundleError` on any failure.
    """
    # 1. Pydantic validation.
    try:
        bundle = BundleSchema.model_validate(d)
    except Exception as exc:
        # Pydantic ValidationError has a ``.errors()`` method
        # that returns a list of error dicts. Call it.
        errors_callable = getattr(exc, "errors", None)
        errors: list[dict[str, Any]] = []
        if callable(errors_callable):
            try:
                errors = list(errors_callable())
            except Exception:
                errors = []
        if errors:
            e = errors[0]
            loc = ".".join(str(p) for p in e.get("loc", ()))
            msg = e.get("msg", str(exc))
            raise ConcordoBundleError(
                bundle_id=d.get("bundle_id") if isinstance(d, dict) else None,
                path=loc or "<root>",
                message=msg,
                hint=e.get("type"),
            ) from exc
        raise ConcordoBundleError(
            bundle_id=d.get("bundle_id") if isinstance(d, dict) else None,
            path="<root>",
            message=str(exc),
        ) from exc

    # 2. Cross-reference validation.
    _validate_cross_references(bundle)

    # 3. Build runtime objects.
    fsm_config = (
        _build_fsm_config(bundle.business_fsm, bundle.bundle_id)
        if bundle.business_fsm is not None
        else None
    )
    saga_configs = tuple(
        _build_saga_config(s, bundle.bundle_id) for s in bundle.workflow_sagas
    )

    # 4. Collect raw spec expressions (id -> expression string).
    #    Resolution to Specification instances is the
    #    caller's job (via _mini_lang + SpecRegistry).
    specifications = {spec.id: spec.expression for spec in bundle.specifications}

    return LoadedBundle(
        bundle_id=bundle.bundle_id,
        events=tuple(bundle.events),
        specifications=specifications,
        fsm=fsm_config,
        sagas=saga_configs,
    )


def load_bundle_yaml(path: str | Path) -> LoadedBundle:
    """Read a YAML file and load it as a bundle."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"bundle file not found: {p}")
    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for YAML bundle loading; "
            "install it via `pip install pyyaml`"
        ) from exc
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConcordoBundleError(
            bundle_id=None,
            path="<root>",
            message=(
                f"top-level YAML in {p} must be a mapping, got {type(data).__name__}"
            ),
        )
    return load_bundle_dict(data)


def load_bundle_json(path: str | Path) -> LoadedBundle:
    """Read a JSON file and load it as a bundle.

    JSON is a strict subset of YAML; the same loader
    handles both via the underlying dict.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"bundle file not found: {p}")
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConcordoBundleError(
            bundle_id=None,
            path="<root>",
            message=(
                f"top-level JSON in {p} must be a mapping, got {type(data).__name__}"
            ),
        )
    return load_bundle_dict(data)
