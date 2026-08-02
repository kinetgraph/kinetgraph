# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def test_knt_new_agent(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        # 1. Initialize a project
        init_result = runner.invoke(app, ["init", "project", "my_app"])
        assert init_result.exit_code == 0

        # 2. cd into the project
        os.chdir(tmp_path / "my_app")

        # 3. Create the agent
        result = runner.invoke(app, ["new", "agent", "sales.CheckoutAgent"])

        if result.exit_code != 0:
            print(f"FAILED WITH OUTPUT: {result.stdout}")
            print(f"EXCEPTION: {result.exception}")

        assert result.exit_code == 0
        assert "Generated Agent" in result.stdout

        # 4. Assert the file was created in the correct context
        expected_file = Path("src/my_app/contexts/sales/agents/checkout_agent.py")
        assert expected_file.is_file()

        # 5. Assert the content looks like an Agent config
        # (ADR-053: the historical ``CapabilityPolicy`` decorator
        # class was removed in v0.9.0 / ADR-039; the new template
        # emits a documentation-only event allow-list instead).
        content = expected_file.read_text()
        assert "get_checkout_agent_allowed_events" in content
        assert "get_checkout_agent_systems" in content
        assert "get_checkout_agent_tools" in content
        # The CapabilityPolicy class is gone (it never existed
        # in the framework); the comment that documents the
        # historical removal is fine.
        assert "from kntgraph.security.authorization import CapabilityPolicy" not in content
        # The agent file does NOT import or instantiate the
        # ``ReactiveDispatcher`` (the dispatcher is wired in
        # ``<context>/dispatcher.py``, not in the agent file).
        # The word may appear in the docstring (the runtime-
        # enforced allow-list recipe mentions it), but the
        # **import** must not be present.
        assert "from kntgraph.runner import ReactiveDispatcher" not in content
        assert "ReactiveDispatcher(" not in content
        assert "ReactiveDispatcher =" not in content

    finally:
        os.chdir(current_dir)
