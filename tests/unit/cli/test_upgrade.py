# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``knt upgrade`` (ADR-053).

The command answers one question: is the generated boilerplate
still what the current templates render? Two of the templates
are **derived** from the bounded context (ADR-088) -- the agent
declaration and the dispatcher -- so the drift check compares
them against a rendering that depends on the context's other
artifacts.
"""

import os
from pathlib import Path

from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def _scaffold(tmp_path: Path) -> Path:
    """Initialise a project with one bounded context and an agent."""
    os.chdir(tmp_path)
    assert runner.invoke(app, ["init", "project", "my_app"]).exit_code == 0
    project = tmp_path / "my_app"
    os.chdir(project)
    assert runner.invoke(app, ["new", "context", "sales"]).exit_code == 0
    assert runner.invoke(app, ["new", "tool", "sales.ProcessPayment"]).exit_code == 0
    assert runner.invoke(app, ["new", "agent", "sales.CheckoutAgent"]).exit_code == 0
    return project


def test_knt_upgrade_check_reports_no_drift(tmp_path: Path) -> None:
    """A freshly scaffolded project is in sync with the templates."""
    current_dir = os.getcwd()
    try:
        _scaffold(tmp_path)

        result = runner.invoke(app, ["upgrade", "check"])
        assert result.exit_code == 0, result.stdout
        assert "No drift detected" in result.stdout
    finally:
        os.chdir(current_dir)


def test_knt_upgrade_check_reports_an_edited_file(tmp_path: Path) -> None:
    """An edited boilerplate file is drift."""
    current_dir = os.getcwd()
    try:
        _scaffold(tmp_path)

        agent = Path("src/my_app/contexts/sales/agents/checkout_agent.py")
        agent.write_text(agent.read_text() + "\n# hand written\n")

        result = runner.invoke(app, ["upgrade", "check"])
        assert result.exit_code == 1
        assert "Drift detected" in result.stdout
        assert "1 file(s) drifted" in result.stdout
    finally:
        os.chdir(current_dir)


def test_knt_upgrade_check_reports_a_drifted_dispatcher(tmp_path: Path) -> None:
    """The dispatcher is derived -- a stale wiring is drift.

    The rendering depends on the context's tools, so a
    dispatcher that predates a tool is out of date.
    """
    current_dir = os.getcwd()
    try:
        _scaffold(tmp_path)

        dispatcher = Path("src/my_app/contexts/sales/dispatcher.py")
        dispatcher.write_text(
            dispatcher.read_text().replace(
                "worker_manager.register(ProcessPayment(), acl=default_acl())",
                "# the tool is not registered",
            )
        )

        result = runner.invoke(app, ["upgrade", "check"])
        assert result.exit_code == 1
        assert "dispatcher.py" in result.stdout
    finally:
        os.chdir(current_dir)


def test_knt_upgrade_apply_regenerates_the_file(tmp_path: Path) -> None:
    """``knt upgrade apply`` rewrites a drifted file."""
    current_dir = os.getcwd()
    try:
        _scaffold(tmp_path)

        agent = Path("src/my_app/contexts/sales/agents/checkout_agent.py")
        agent.write_text(agent.read_text() + "\n# hand written\n")

        result = runner.invoke(
            app,
            [
                "upgrade",
                "apply",
                "src/my_app/contexts/sales/agents/checkout_agent.py",
                "--force",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "# hand written" not in agent.read_text()
        assert "CHECKOUT_AGENT = AgentDefinition(" in agent.read_text()

        # The regenerated file is in sync again.
        assert runner.invoke(app, ["upgrade", "check"]).exit_code == 0
    finally:
        os.chdir(current_dir)


def test_knt_upgrade_check_ignores_the_http_only_files(tmp_path: Path) -> None:
    """A project without the HTTP gateway has no phantom drift.

    ``consumer.py`` and ``core/config.py`` belong to the
    ``--use-intent-http`` scaffold; listing them in every
    project made the check report a drift that no regeneration
    could fix.
    """
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert runner.invoke(app, ["init", "project", "my_app"]).exit_code == 0
        os.chdir(tmp_path / "my_app")

        result = runner.invoke(app, ["upgrade", "check"])
        assert result.exit_code == 0, result.stdout
        assert "No drift detected" in result.stdout
    finally:
        os.chdir(current_dir)
