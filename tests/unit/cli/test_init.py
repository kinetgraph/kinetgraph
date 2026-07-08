import os
from pathlib import Path
from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()

def test_knt_init_scaffolds_project(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "my_project"])
    finally:
        os.chdir(current_dir)
        
    if result.exit_code != 0:
        print(f"FAILED WITH OUTPUT: {result.stdout}")
        print(f"EXCEPTION: {result.exception}")
    assert result.exit_code == 0
    assert "Initializing Kinetgraph project: my_project" in result.stdout
    
    # Assert directories
    project_dir = tmp_path / "my_project"
    assert (project_dir / "src" / "my_project" / "core").is_dir()
    assert (project_dir / "src" / "my_project" / "contexts").is_dir()
    assert (project_dir / "tests" / "unit").is_dir()
    assert (project_dir / "tests" / "integration").is_dir()
    
    # Assert files
    assert (project_dir / "pyproject.toml").is_file()
    assert (project_dir / ".env.example").is_file()
    assert (project_dir / "src" / "my_project" / "main.py").is_file()
