# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the bundle loader (ADR-073 §5).
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from kntgraph.concordos import (
    ConcordoBundleError,
    ConcordoCatalog,
    LoadedBundle,
    load_bundle_dict,
    load_bundle_json,
    load_bundle_yaml,
)
from kntgraph.concordos.saga import SagaConfig
from kntgraph.concordos.fsm import FSMConfig


# ---------------------------------------------------------------------------
# Fixtures: a small DomainComponent class reachable via dotted path
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _install_test_module():
    """Create a fake module path with a known DomainComponent
    subclass so the loader's dotted-path resolution works.
    """
    mod_name = "_kntgraph_loader_test_module"
    if mod_name not in sys.modules:
        # Build a small module that exposes:
        #   - InvoiceComponent (DomainComponent subclass)
        #   - OrderComponent (DomainComponent subclass)
        from kntgraph.core.world import DomainComponent
        from dataclasses import dataclass

        @dataclass(frozen=True, slots=True)
        class InvoiceComponent(DomainComponent):
            status: str = "draft"
            amount: int = 0

        @dataclass(frozen=True, slots=True)
        class OrderComponent(DomainComponent):
            state: str = "new"

        mod = types.ModuleType(mod_name)
        mod.InvoiceComponent = InvoiceComponent
        mod.OrderComponent = OrderComponent
        sys.modules[mod_name] = mod

    return mod_name


def _valid_bundle(
    component_path: str = "_kntgraph_loader_test_module.InvoiceComponent",
) -> dict:
    return {
        "bundle_id": "com.acme.test",
        "version": "1.0.0",
        "events": [
            {"name": "invoice.submitted", "schema": "builtins.dict"},
            {"name": "invoice.approved", "schema": "builtins.dict"},
            {"name": "invoice.issuance_confirmed", "schema": "builtins.dict"},
        ],
        "specifications": [
            {"id": "ConfidencePassed", "expression": "event.data.score >= 0.80"},
        ],
        "business_fsm": {
            "id": "fsm:Invoice",
            "component": component_path,
            "state_field": "status",
            "initial_state": "draft",
            "states": ["draft", "validating", "issued"],
            "terminal": ["issued"],
            "transitions": [
                {"from": "draft", "to": "validating", "on_event": "invoice.submitted"},
                {
                    "from": "validating",
                    "to": "issued",
                    "on_event": "invoice.approved",
                },
            ],
            "on_entry": {"issued": "invoice.issuance_confirmed"},
        },
        "workflow_sagas": [
            {
                "id": "saga:EntityExtraction",
                "trigger_event": "invoice.submitted",
                "steps": [
                    {
                        "name": "extract",
                        "tool": "gliner2_extract",
                        "timeout_ms": 30000,
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_minimal_bundle(self) -> None:
        d = {
            "bundle_id": "com.acme.min",
            "version": "1.0.0",
            "events": [],
            "specifications": [],
            "workflow_sagas": [],
        }
        loaded = load_bundle_dict(d)
        assert isinstance(loaded, LoadedBundle)
        assert loaded.bundle_id == "com.acme.min"
        assert loaded.fsm is None
        assert loaded.sagas == ()

    def test_full_bundle(self, _install_test_module) -> None:
        d = _valid_bundle()
        loaded = load_bundle_dict(d)
        assert loaded.bundle_id == "com.acme.test"
        assert loaded.fsm is not None
        assert isinstance(loaded.fsm, FSMConfig)
        assert len(loaded.sagas) == 1
        assert isinstance(loaded.sagas[0], SagaConfig)
        assert loaded.sagas[0].name == "EntityExtraction"

    def test_specifications_carried(self, _install_test_module) -> None:
        d = _valid_bundle()
        loaded = load_bundle_dict(d)
        assert "ConfidencePassed" in loaded.specifications
        assert loaded.specifications["ConfidencePassed"] == "event.data.score >= 0.80"

    def test_from_yaml(self, _install_test_module, tmp_path) -> None:
        d = _valid_bundle()
        path = tmp_path / "bundle.yaml"
        # PyYAML might not be in env; fall back to JSON.
        try:
            import yaml  # noqa: F401
        except ImportError:
            path.write_text(json.dumps(d))
            loaded = load_bundle_yaml(path)
        else:
            path.write_text(yaml.safe_dump(d))
            loaded = load_bundle_yaml(path)
        assert loaded.bundle_id == "com.acme.test"

    def test_from_json(self, _install_test_module, tmp_path) -> None:
        d = _valid_bundle()
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(d))
        loaded = load_bundle_json(path)
        assert loaded.bundle_id == "com.acme.test"

    def test_from_catalog(self, _install_test_module, tmp_path) -> None:
        d = _valid_bundle()
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(d))
        catalog = ConcordoCatalog.from_json(path)
        assert len(catalog._concordos) == 2  # 1 FSM + 1 saga
        names = set(catalog._concordos.keys())
        # FSM name is derived from the component class name
        # (BusinessFSMConcordo convention).
        assert "fsm:InvoiceComponent" in names
        # Saga name is derived from the schema id minus prefix.
        assert "saga:EntityExtraction" in names


# ---------------------------------------------------------------------------
# Pydantic validation errors
# ---------------------------------------------------------------------------


class TestPydanticValidation:
    def test_unknown_field_rejected(self) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0.0",
            "events": [],
            "specifications": [],
            "workflow_sagas": [],
            "extra_field": "x",
        }
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_dict(d)
        assert "extra_field" in str(exc_info.value)

    def test_invalid_bundle_id_pattern(self) -> None:
        d = {
            "bundle_id": "Acme_Test",  # uppercase
            "version": "1.0.0",
            "events": [],
            "specifications": [],
            "workflow_sagas": [],
        }
        with pytest.raises(ConcordoBundleError):
            load_bundle_dict(d)

    def test_invalid_version_pattern(self) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0",  # not semver
            "events": [],
            "specifications": [],
            "workflow_sagas": [],
        }
        with pytest.raises(ConcordoBundleError):
            load_bundle_dict(d)


# ---------------------------------------------------------------------------
# Cross-reference errors
# ---------------------------------------------------------------------------


class TestCrossReference:
    def test_unknown_event_in_transition(self) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0.0",
            "events": [{"name": "invoice.submitted", "schema": "builtins.dict"}],
            "specifications": [],
            "business_fsm": {
                "id": "fsm:Invoice",
                "component": "_kntgraph_loader_test_module.InvoiceComponent",
                "state_field": "status",
                "initial_state": "draft",
                "states": ["draft", "issued"],
                "terminal": ["issued"],
                "transitions": [
                    {
                        "from": "draft",
                        "to": "issued",
                        "on_event": "invoice.unknown",  # not declared
                    }
                ],
            },
            "workflow_sagas": [],
        }
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_dict(d)
        err = str(exc_info.value)
        assert "invoice.unknown" in err
        assert "transitions" in err  # dotted path to the offending field

    def test_unknown_event_in_on_entry(self) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0.0",
            "events": [{"name": "invoice.approved", "schema": "builtins.dict"}],
            "specifications": [],
            "business_fsm": {
                "id": "fsm:Invoice",
                "component": "_kntgraph_loader_test_module.InvoiceComponent",
                "state_field": "status",
                "initial_state": "draft",
                "states": ["draft", "issued"],
                "terminal": ["issued"],
                "transitions": [
                    {"from": "draft", "to": "issued", "on_event": "invoice.approved"},
                ],
                "on_entry": {"issued": "invoice.issued_wrong"},
            },
            "workflow_sagas": [],
        }
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_dict(d)
        assert "invoice.issued_wrong" in str(exc_info.value)

    def test_unknown_trigger_event_in_saga(self) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0.0",
            "events": [
                {"name": "invoice.submitted", "schema": "builtins.dict"},
                {"name": "invoice.extracted", "schema": "builtins.dict"},
            ],
            "specifications": [],
            "workflow_sagas": [
                {
                    "id": "saga:Extraction",
                    "trigger_event": "invoice.unknown",
                    "steps": [{"name": "extract", "tool": "gliner2"}],
                }
            ],
        }
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_dict(d)
        assert "invoice.unknown" in str(exc_info.value)
        assert "workflow_sagas" in str(exc_info.value)

    def test_duplicate_specification_id(self) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0.0",
            "events": [],
            "specifications": [
                {"id": "SameId", "expression": "x == 1"},
                {"id": "SameId", "expression": "y == 2"},  # duplicate
            ],
            "workflow_sagas": [],
        }
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_dict(d)
        assert "duplicate" in str(exc_info.value).lower()
        assert "SameId" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Dotted-path resolution
# ---------------------------------------------------------------------------


class TestDottedPathResolution:
    def test_unknown_module(self) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0.0",
            "events": [
                {"name": "invoice.submitted", "schema": "builtins.dict"},
            ],
            "specifications": [],
            "business_fsm": {
                "id": "fsm:Invoice",
                "component": "this.module.does.not.exist.InvoiceComponent",
                "state_field": "status",
                "initial_state": "draft",
                "states": ["draft", "issued"],
                "transitions": [
                    {"from": "draft", "to": "issued", "on_event": "invoice.submitted"}
                ],
            },
            "workflow_sagas": [],
        }
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_dict(d)
        assert "this.module.does.not.exist" in str(exc_info.value)

    def test_unknown_attribute(self, _install_test_module) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0.0",
            "events": [
                {"name": "invoice.submitted", "schema": "builtins.dict"},
            ],
            "specifications": [],
            "business_fsm": {
                "id": "fsm:Invoice",
                "component": "_kntgraph_loader_test_module.NonExistentClass",
                "state_field": "status",
                "initial_state": "draft",
                "states": ["draft", "issued"],
                "transitions": [
                    {"from": "draft", "to": "issued", "on_event": "invoice.submitted"}
                ],
            },
            "workflow_sagas": [],
        }
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_dict(d)
        assert "NonExistentClass" in str(exc_info.value)

    def test_invalid_dotted_path(self) -> None:
        d = {
            "bundle_id": "com.acme",
            "version": "1.0.0",
            "events": [
                {"name": "invoice.submitted", "schema": "builtins.dict"},
            ],
            "specifications": [],
            "business_fsm": {
                "id": "fsm:Invoice",
                "component": "not-a-dotted-path",
                "state_field": "status",
                "initial_state": "draft",
                "states": ["draft", "issued"],
                "transitions": [
                    {"from": "draft", "to": "issued", "on_event": "invoice.submitted"}
                ],
            },
            "workflow_sagas": [],
        }
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_dict(d)
        assert "dotted path" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# File-not-found
# ---------------------------------------------------------------------------


class TestFileIO:
    def test_yaml_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_bundle_yaml("/nonexistent/path.yaml")

    def test_json_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_bundle_json("/nonexistent/path.json")

    def test_yaml_must_be_mapping(self, tmp_path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- 1\n- 2\n")  # top-level list, not mapping
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_yaml(path)
        assert "mapping" in str(exc_info.value).lower()

    def test_json_must_be_mapping(self, tmp_path) -> None:
        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ConcordoBundleError) as exc_info:
            load_bundle_json(path)
        assert "mapping" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_loaded_bundle_builds_into_concordos(self, _install_test_module) -> None:
        """The LoadedBundle's runtime objects are wrapped
        into Concordos by the catalog's classmethods."""
        d = _valid_bundle()
        loaded = load_bundle_dict(d)
        assert loaded.fsm is not None
        assert loaded.sagas and loaded.sagas[0].name == "EntityExtraction"
        # The Concordos are accessible after wrapping.
        catalog = ConcordoCatalog.from_dict(d)
        names = set(catalog._concordos.keys())
        assert "fsm:InvoiceComponent" in names
        assert "saga:EntityExtraction" in names
