# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path

from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def _bootstrap_project(tmp_path: Path, package_name: str = "demo_app") -> Path:
    project_dir = tmp_path / package_name
    (project_dir / "src" / package_name).mkdir(parents=True)
    (project_dir / "src" / package_name / "__init__.py").write_text(
        "# package marker\n", encoding="utf-8"
    )
    return project_dir


def _invoke_from_project(project_dir: Path, *args: str):
    current_dir = os.getcwd()
    try:
        os.chdir(project_dir)
        return runner.invoke(app, list(args))
    finally:
        os.chdir(current_dir)


def test_new_system_command_creates_expected_artifact(tmp_path: Path):
    project_dir = _bootstrap_project(tmp_path)

    result = _invoke_from_project(project_dir, "new", "system", "sales.CheckoutSystem")

    assert result.exit_code == 0
    generated_file = (
        project_dir
        / "src"
        / "demo_app"
        / "contexts"
        / "sales"
        / "systems"
        / "checkout_system.py"
    )
    assert generated_file.is_file()
    assert "Pure WorldSystem" in generated_file.read_text(encoding="utf-8")


def test_new_context_and_agent_commands_generate_structures(tmp_path: Path):
    project_dir = _bootstrap_project(tmp_path)

    context_result = _invoke_from_project(project_dir, "new", "context", "sales")
    agent_result = _invoke_from_project(
        project_dir, "new", "agent", "sales.CheckoutAgent"
    )

    assert context_result.exit_code == 0
    assert agent_result.exit_code == 0

    context_dir = project_dir / "src" / "demo_app" / "contexts" / "sales"
    assert (context_dir / "dispatcher.py").is_file()
    assert (context_dir / "agents" / "checkout_agent.py").is_file()
    assert (context_dir / "agents" / "__init__.py").is_file()


def test_new_component_event_and_tool_commands_create_files(tmp_path: Path):
    project_dir = _bootstrap_project(tmp_path)

    component_result = _invoke_from_project(
        project_dir, "new", "component", "sales.CartItem"
    )
    event_result = _invoke_from_project(
        project_dir, "new", "event", "sales.OrderPlaced"
    )
    tool_result = _invoke_from_project(
        project_dir, "new", "tool", "sales.ProcessPayment"
    )

    assert component_result.exit_code == 0
    assert event_result.exit_code == 0
    assert tool_result.exit_code == 0

    context_dir = project_dir / "src" / "demo_app" / "contexts" / "sales"
    assert (context_dir / "components" / "cart_item.py").is_file()
    assert (context_dir / "events" / "order_placed.py").is_file()
    assert (context_dir / "tools" / "process_payment.py").is_file()


def test_keys_generate_writes_files_to_disk(tmp_path: Path):
    project_dir = _bootstrap_project(tmp_path)

    out_dir = project_dir / "keys"
    result = _invoke_from_project(
        project_dir,
        "keys",
        "generate",
        "--agent-id",
        "agent-1",
        "--out-dir",
        str(out_dir),
    )

    assert result.exit_code == 0
    assert (out_dir / "agent-1_private.pem").is_file()
    assert (out_dir / "agent-1_public.pem").is_file()


def test_keys_generate_prints_pem_to_stdout(tmp_path: Path):
    project_dir = _bootstrap_project(tmp_path)

    result = _invoke_from_project(
        project_dir, "keys", "generate", "--agent-id", "agent-2"
    )

    assert result.exit_code == 0
    assert "BEGIN PRIVATE KEY" in result.stdout
    assert "BEGIN PUBLIC KEY" in result.stdout
