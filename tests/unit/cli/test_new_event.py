# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def test_knt_new_event(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        # 1. Initialize a project to get the structure
        init_result = runner.invoke(app, ["init", "my_app"])
        assert init_result.exit_code == 0

        # 2. cd into the project
        os.chdir(tmp_path / "my_app")

        # 3. Create the event
        result = runner.invoke(app, ["new", "event", "sales.OrderPlaced"])

        if result.exit_code != 0:
            print(f"FAILED WITH OUTPUT: {result.stdout}")
            print(f"EXCEPTION: {result.exception}")

        assert result.exit_code == 0
        assert "Generated Event factory" in result.stdout

        # 4. Assert the file was created in the correct context
        expected_file = Path("src/my_app/contexts/sales/events/order_placed.py")
        assert expected_file.is_file()

        # 5. Assert the content looks like an Event factory
        content = expected_file.read_text()
        assert "def order_placed(" in content
        assert "Event.domain_from(" in content
        assert 'type="sales.order_placed"' in content
        assert "correlation=" in content

    finally:
        os.chdir(current_dir)
