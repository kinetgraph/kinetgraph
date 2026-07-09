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

def test_knt_init_with_http(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init", "my_app_http", "--use-intent-http"])
        
        assert result.exit_code == 0
        assert "Success" in result.stdout
        
        # Verify pyproject.toml has fastapi and uvicorn
        pyproject_content = (tmp_path / "my_app_http" / "pyproject.toml").read_text()
        assert "kntgraph[api]" in pyproject_content
        assert "uvicorn" in pyproject_content
        
        # Verify main.py has the FastAPI lifespan code
        main_content = (tmp_path / "my_app_http" / "src" / "my_app_http" / "main.py").read_text()
        assert "from fastapi import FastAPI" not in main_content # create_app abstracts this
        assert "from kntgraph.api import create_app" in main_content
        assert "@asynccontextmanager" in main_content
        assert "uvicorn.run(" in main_content
        
    finally:
        os.chdir(current_dir)
