# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the bundle-format Pydantic schemas (ADR-073).

The schemas are strict (``extra='forbid'``): typos in field
names fail the load with the dotted path. This test
pins that contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kntgraph.concordos.schemas import (
    BundleSchema,
    EventSchema,
    FSMConfigSchema,
    FSMTransitionSchema,
    SagaConfigSchema,
    SagaStepSchema,
    SpecificationSchema,
)


class TestEventSchema:
    def test_valid_event(self) -> None:
        e = EventSchema(
            name="invoice.submitted",
            schema="acme.events.InvoiceSubmitted",
        )
        assert e.name == "invoice.submitted"
        assert e.schema_ == "acme.events.InvoiceSubmitted"

    def test_single_segment_name_rejected(self) -> None:
        """Event names need at least two segments (domain.name)."""
        with pytest.raises(ValidationError):
            EventSchema(name="submitted", schema="acme.InvoiceSubmitted")

    def test_uppercase_name_rejected(self) -> None:
        """Event names must be lowercase."""
        with pytest.raises(ValidationError):
            EventSchema(
                name="Invoice.submitted", schema="acme.events.InvoiceSubmitted"
            )

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EventSchema(
                name="invoice.submitted",
                schema="acme.events.InvoiceSubmitted",
                extra_field="x",  # type: ignore[call-arg]
            )


class TestSpecificationSchema:
    def test_valid_specification(self) -> None:
        s = SpecificationSchema(
            id="ConfidencePassed",
            expression="event.data.confidence_score >= 0.80",
        )
        assert s.id == "ConfidencePassed"

    def test_numeric_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SpecificationSchema(id="1ConfidencePassed", expression="x")

    def test_empty_expression_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SpecificationSchema(id="Foo", expression="")


class TestFSMTransitionSchema:
    def test_valid_transition(self) -> None:
        t = FSMTransitionSchema(
            **{"from": "draft", "to": "validating", "on_event": "invoice.submitted"}
        )
        assert t.from_state == "draft"
        assert t.to == "validating"
        assert t.on_event == "invoice.submitted"
        assert t.guard is None

    def test_optional_guard(self) -> None:
        t = FSMTransitionSchema(
            **{
                "from": "validating",
                "to": "issued",
                "on_event": "invoice.approved",
                "guard": "event.data.amount > 1000",
            }
        )
        assert t.guard == "event.data.amount > 1000"


class TestFSMConfigSchema:
    @staticmethod
    def _minimal_fsm() -> dict:
        return {
            "id": "fsm:Invoice",
            "component": "acme.InvoiceComponent",
            "state_field": "status",
            "initial_state": "draft",
            "states": ["draft", "validating", "issued"],
            "terminal": ["issued"],
            "transitions": [
                {"from": "draft", "to": "validating", "on_event": "invoice.submitted"},
            ],
        }

    def test_valid_fsm(self) -> None:
        fsm = FSMConfigSchema(**self._minimal_fsm())
        assert fsm.id == "fsm:Invoice"
        assert fsm.states == ["draft", "validating", "issued"]

    def test_initial_state_must_be_in_states(self) -> None:
        # Initial state is in 'states' (Pydantic doesn't
        # check this — that's the loader's job). The
        # schema itself accepts any string matching IDENT.
        # Cross-validation is in the loader, not here.
        fsm = self._minimal_fsm()
        fsm["initial_state"] = "phantom"
        schema = FSMConfigSchema(**fsm)
        assert schema.initial_state == "phantom"  # schema accepts
        # Loader would reject.

    def test_terminal_subset_of_states_enforced(self) -> None:
        fsm = self._minimal_fsm()
        fsm["terminal"] = ["phantom"]
        with pytest.raises(ValidationError) as exc_info:
            FSMConfigSchema(**fsm)
        assert "terminal states not declared" in str(exc_info.value)

    def test_id_must_have_fsm_prefix(self) -> None:
        fsm = self._minimal_fsm()
        fsm["id"] = "Invoice"  # no fsm: prefix
        with pytest.raises(ValidationError):
            FSMConfigSchema(**fsm)

    def test_unknown_field_rejected(self) -> None:
        fsm = self._minimal_fsm()
        fsm["extra_top_level"] = "x"
        with pytest.raises(ValidationError):
            FSMConfigSchema(**fsm)


class TestSagaStepSchema:
    def test_valid_step(self) -> None:
        s = SagaStepSchema(name="text_chunking", tool="chunker")
        assert s.name == "text_chunking"
        assert s.tool == "chunker"

    def test_human_step_with_no_tool(self) -> None:
        """``tool: None`` declares a human step (ADR-072)."""
        s = SagaStepSchema(name="approve", approval_timeout_ms=3600000)
        assert s.tool is None
        assert s.approval_timeout_ms == 3600000

    def test_input_mapping_validated(self) -> None:
        """Paths must start with event.data., steps., or agent."""
        with pytest.raises(ValidationError):
            SagaStepSchema(
                name="x",
                tool="chunker",
                input_mapping={"bad": "wrong.scope"},
            )

    def test_valid_input_mapping_paths(self) -> None:
        s = SagaStepSchema(
            name="x",
            tool="chunker",
            input_mapping={
                "text": "event.data.content",
                "chunks": "steps.Chunking.output.chunks",
                "user": "agent.user_id",
            },
        )
        assert "event.data.text" not in s.input_mapping  # sanity
        assert s.input_mapping["text"] == "event.data.content"


class TestSagaConfigSchema:
    @staticmethod
    def _minimal_saga() -> dict:
        return {
            "id": "saga:EntityExtraction",
            "trigger_event": "document.ingested",
            "steps": [
                {"name": "chunk", "tool": "chunker"},
                {"name": "extract", "tool": "gliner2"},
            ],
        }

    def test_valid_saga(self) -> None:
        saga = SagaConfigSchema(**self._minimal_saga())
        assert saga.id == "saga:EntityExtraction"
        assert len(saga.steps) == 2

    def test_unique_step_names_enforced(self) -> None:
        saga = self._minimal_saga()
        saga["steps"].append(
            {"name": "chunk", "tool": "other_chunker"}
        )
        with pytest.raises(ValidationError) as exc_info:
            SagaConfigSchema(**saga)
        assert "duplicate step names" in str(exc_info.value)

    def test_id_must_have_saga_prefix(self) -> None:
        saga = self._minimal_saga()
        saga["id"] = "EntityExtraction"
        with pytest.raises(ValidationError):
            SagaConfigSchema(**saga)


class TestBundleSchema:
    def test_minimal_bundle(self) -> None:
        b = BundleSchema(
            bundle_id="com.acme.test", version="1.0.0"
        )
        assert b.bundle_id == "com.acme.test"
        assert b.events == []
        assert b.business_fsm is None
        assert b.workflow_sagas == []

    def test_full_bundle(self) -> None:
        b = BundleSchema(
            bundle_id="com.acme.knowledge",
            version="1.0.0",
            events=[
                EventSchema(
                    name="document.ingested",
                    schema="acme.events.DocumentIngested",
                ),
            ],
            specifications=[
                SpecificationSchema(
                    id="ConfidencePassed",
                    expression="event.data.confidence_score >= 0.80",
                ),
            ],
            business_fsm=FSMConfigSchema(
                id="fsm:Knowledge",
                component="acme.components.KnowledgeComponent",
                state_field="lifecycle_state",
                initial_state="INGESTED",
                states=["INGESTED", "CONSOLIDATED"],
                terminal=["CONSOLIDATED"],
                transitions=[
                    {
                        "from": "INGESTED",
                        "to": "CONSOLIDATED",
                        "on_event": "document.ingested",
                        "guard": "ConfidencePassed",
                    },
                ],
            ),
            workflow_sagas=[
                SagaConfigSchema(
                    id="saga:Extraction",
                    trigger_event="document.ingested",
                    steps=[{"name": "extract", "tool": "gliner2"}],
                ),
            ],
        )
        assert len(b.events) == 1
        assert b.business_fsm is not None
        assert b.business_fsm.id == "fsm:Knowledge"
        assert len(b.workflow_sagas) == 1

    def test_bundle_id_pattern(self) -> None:
        """Reverse-DNS pattern required."""
        with pytest.raises(ValidationError):
            BundleSchema(bundle_id="Acme_Test", version="1.0.0")

    def test_version_must_be_semver(self) -> None:
        with pytest.raises(ValidationError):
            BundleSchema(bundle_id="com.acme", version="1.0")

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BundleSchema(
                bundle_id="com.acme",
                version="1.0.0",
                extra_field="x",  # type: ignore[call-arg]
            )
