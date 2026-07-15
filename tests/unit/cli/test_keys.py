# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def test_knt_keys_generate_stdout():
    result = runner.invoke(app, ["keys", "generate", "--agent-id", "123"])

    if result.exit_code != 0:
        print(f"FAILED WITH OUTPUT: {result.stdout}")
        print(f"EXCEPTION: {result.exception}")

    assert result.exit_code == 0
    # Should output two PEMs
    assert "BEGIN PRIVATE KEY" in result.stdout
    assert "BEGIN PUBLIC KEY" in result.stdout


def test_knt_keys_generate_out_dir(tmp_path: Path):
    result = runner.invoke(
        app, ["keys", "generate", "--agent-id", "123", "--out-dir", str(tmp_path)]
    )

    assert result.exit_code == 0

    priv_file = tmp_path / "123_private.pem"
    pub_file = tmp_path / "123_public.pem"

    assert priv_file.is_file()
    assert pub_file.is_file()

    assert "BEGIN PRIVATE KEY" in priv_file.read_text()
    assert "BEGIN PUBLIC KEY" in pub_file.read_text()
