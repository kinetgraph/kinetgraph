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
        init_result = runner.invoke(app, ["init", "my_app"])
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
        content = expected_file.read_text()
        assert "CapabilityPolicy" in content
        assert "build_checkout_agent_policy" in content
        assert "get_checkout_agent_systems" in content
        assert "get_checkout_agent_tools" in content
        assert "ReactiveDispatcher" not in content # Removed dispatcher logic
        
    finally:
        os.chdir(current_dir)
