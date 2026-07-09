import os
from pathlib import Path
from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def test_knt_new_context(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        # 1. Initialize a project
        init_result = runner.invoke(app, ["init", "my_app"])
        assert init_result.exit_code == 0

        # 2. cd into the project
        os.chdir(tmp_path / "my_app")

        # 3. Create the context
        result = runner.invoke(app, ["new", "context", "sales"])

        if result.exit_code != 0:
            print(f"FAILED WITH OUTPUT: {result.stdout}")
            print(f"EXCEPTION: {result.exception}")

        assert result.exit_code == 0
        assert "Generated Bounded Context 'sales'" in result.stdout

        # 4. Assert the dispatcher file was created
        dispatcher_file = Path("src/my_app/contexts/sales/dispatcher.py")
        assert dispatcher_file.is_file()

        # 5. Assert subdirectories were created
        assert (Path("src/my_app/contexts/sales/agents/__init__.py")).is_file()
        assert (Path("src/my_app/contexts/sales/systems/__init__.py")).is_file()
        assert (Path("src/my_app/contexts/sales/events/__init__.py")).is_file()
        assert (Path("src/my_app/contexts/sales/tools/__init__.py")).is_file()
        assert (Path("src/my_app/contexts/sales/components/__init__.py")).is_file()

        # 6. Assert the content looks like a Context Dispatcher
        content = dispatcher_file.read_text()
        assert "ReactiveDispatcher" in content
        assert "def build_sales_dispatcher" in content
        assert "systems = []" in content
        assert "tools = []" in content

    finally:
        os.chdir(current_dir)
