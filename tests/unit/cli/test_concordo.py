# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def _init_project(tmp_path: Path) -> Path:
    """Initialize a project and return its root path."""
    os.chdir(tmp_path)
    init_result = runner.invoke(app, ["init", "project", "my_app"])
    assert init_result.exit_code == 0
    project = tmp_path / "my_app"
    os.chdir(project)
    return project


def test_knt_concordo_add_fsm(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        _init_project(tmp_path)
        result = runner.invoke(
            app,
            [
                "concordo",
                "add",
                "fsm",
                "InvoiceFSM",
                "--component",
                "InvoiceDomainComponent",
                "--state-field",
                "status",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "Generated FSM Concordo" in result.stdout

        expected = Path("src/my_app/concordos/invoice_fsm_concordo.py")
        assert expected.is_file()
        content = expected.read_text()
        assert "BusinessFSMConcordo" in content
        assert "InvoiceDomainComponent" in content
        assert 'state_field="status"' in content
    finally:
        os.chdir(current_dir)


def test_knt_concordo_new_saga(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        _init_project(tmp_path)
        result = runner.invoke(
            app,
            [
                "concordo",
                "new",
                "saga",
                "NfeEmission",
                "--steps",
                "validate_fiscal:sefaz_validator,emit_nfe:nfe_emitter",
                "--timeout-ms",
                "300000",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "Generated Saga Concordo" in result.stdout

        expected = Path("src/my_app/concordos/nfe_emission_concordo.py")
        assert expected.is_file()
        content = expected.read_text()
        assert "WorkflowSagaConcordo" in content
        assert 'name="nfe_emission"' in content
        assert "sefaz_validator" in content
        assert "nfe_emitter" in content
        assert "saga_timeout_ms=300000" in content
    finally:
        os.chdir(current_dir)


def test_knt_concordo_generates_stub_test(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        _init_project(tmp_path)
        result = runner.invoke(
            app,
            [
                "concordo",
                "add",
                "fsm",
                "InvoiceFSM",
                "--component",
                "InvoiceDomainComponent",
            ],
        )
        assert result.exit_code == 0, result.stdout
        test_file = Path("tests/unit/concordos/test_invoice_fsm_concordo.py")
        assert test_file.is_file()
        content = test_file.read_text()
        assert "InvoiceFSMConcordo" in content
    finally:
        os.chdir(current_dir)
