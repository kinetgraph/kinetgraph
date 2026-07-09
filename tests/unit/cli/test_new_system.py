import os
from pathlib import Path
from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def test_knt_new_system(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        # 1. Initialize a project to get the structure
        init_result = runner.invoke(app, ["init", "my_app"])
        assert init_result.exit_code == 0

        # 2. cd into the project
        os.chdir(tmp_path / "my_app")

        # 3. Create the system
        result = runner.invoke(app, ["new", "system", "sales.CheckoutSystem"])

        if result.exit_code != 0:
            print(f"FAILED WITH OUTPUT: {result.stdout}")
            print(f"EXCEPTION: {result.exception}")

        assert result.exit_code == 0
        assert "Generated WorldSystem" in result.stdout

        # 4. Assert the file was created in the correct context
        expected_file = Path("src/my_app/contexts/sales/systems/checkout_system.py")
        assert expected_file.is_file()

        # 5. Assert the content looks like a WorldSystem
        content = expected_file.read_text()
        assert "def checkout_system(" in content
        assert "world: World" in content
        assert "return events" in content
        assert "from kntgraph.core.world import World" in content

    finally:
        os.chdir(current_dir)
