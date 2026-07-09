import os
from pathlib import Path
from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def test_knt_new_tool(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        # 1. Initialize a project to get the structure
        init_result = runner.invoke(app, ["init", "my_app"])
        assert init_result.exit_code == 0

        # 2. cd into the project
        os.chdir(tmp_path / "my_app")

        # 3. Create the tool
        result = runner.invoke(app, ["new", "tool", "sales.ProcessPayment"])

        if result.exit_code != 0:
            print(f"FAILED WITH OUTPUT: {result.stdout}")
            print(f"EXCEPTION: {result.exception}")

        assert result.exit_code == 0
        assert "Generated Tool" in result.stdout

        # 4. Assert the file was created in the correct context
        expected_file = Path("src/my_app/contexts/sales/tools/process_payment.py")
        assert expected_file.is_file()

        # 5. Assert the content looks like a Tool
        content = expected_file.read_text()
        assert "@tool_worker" in content
        assert "class ProcessPayment:" in content
        assert "async def invoke(" in content
        assert "-> Result[" in content

    finally:
        os.chdir(current_dir)
