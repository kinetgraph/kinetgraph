# SPDX-FileCopyrightText: 2026 kinetgraph
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``scripts/design_gate.py`` (ADR-089).

Covers:
  - Clean baseline check: `src/kntgraph` has zero design violations.
  - KNT-BOUND-001: Framework cannot import from verticals.
  - KNT-TYPE-001: TypedDict is prohibited in framework code.
  - KNT-TYPE-002: Wire payload variable `data` cannot be typed `dict[str, Any]`.
  - KNT-ADAPT-001: 3rd-party SDK imports prohibited outside infra/adapters.
  - TYPE_CHECKING exemption: Type checking guards are honored.
  - Vertical-internal imports: Permitted inside vertical packages.
  - Output formats: JSON, LLM, and Terminal formatters.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
DESIGN_GATE_PATH = SCRIPTS_DIR / "design_gate.py"

# Import helpers directly from design_gate for fast unit-level testing
sys.path.insert(0, str(SCRIPTS_DIR))
import design_gate  # noqa: E402


def test_clean_baseline_passes() -> None:
    """Verify that current src/kntgraph passes all design gate rules."""
    violations = design_gate.scan_path(ROOT / "src" / "kntgraph")
    assert not violations, f"Expected 0 violations on clean baseline: {violations}"


def test_boundary_rule_flags_framework_importing_vertical() -> None:
    """KNT-BOUND-001: Framework module importing from vertical must be flagged."""
    bad_code = """
from kntgraph.memory.session import SessionState

def load():
    pass
"""
    fake_file = ROOT / "src" / "kntgraph" / "core" / "_fake_test_module.py"
    rel_path = "src/kntgraph/core/_fake_test_module.py"
    tree = ast.parse(bad_code, filename=str(fake_file))
    violations = design_gate.check_ast(fake_file, tree, rel_path)

    bound_violations = [v for v in violations if v.rule_id == "KNT-BOUND-001"]
    assert len(bound_violations) == 1
    v = bound_violations[0]
    assert "vertical package 'kntgraph.memory.session'" in v.message
    assert "SessionComponent" in v.remedy
    assert "ADR-042" in v.adr


def test_vertical_importing_vertical_is_permitted() -> None:
    """Vertical packages (e.g., agents) importing other verticals is permitted."""
    code = """
from kntgraph.memory.session import SessionState
from kntgraph.knowledge import GraphRag
"""
    fake_file = ROOT / "src" / "kntgraph" / "agents" / "_fake_agent.py"
    rel_path = "src/kntgraph/agents/_fake_agent.py"
    tree = ast.parse(code, filename=str(fake_file))
    violations = design_gate.check_ast(fake_file, tree, rel_path)
    assert not [v for v in violations if v.rule_id == "KNT-BOUND-001"]


def test_typed_dict_prohibited_in_framework() -> None:
    """KNT-TYPE-001: TypedDict in framework must be flagged."""
    bad_code = """
from typing import TypedDict

class MyBag(TypedDict):
    foo: str
"""
    fake_file = ROOT / "src" / "kntgraph" / "stream" / "_fake_stream.py"
    rel_path = "src/kntgraph/stream/_fake_stream.py"
    tree = ast.parse(bad_code, filename=str(fake_file))
    violations = design_gate.check_ast(fake_file, tree, rel_path)

    type_violations = [v for v in violations if v.rule_id == "KNT-TYPE-001"]
    assert len(type_violations) == 1
    v = type_violations[0]
    assert "TypedDict detected" in v.message
    assert "ADR-079" in v.adr


def test_wire_payload_dict_str_any_prohibited() -> None:
    """KNT-TYPE-002: `data: dict[str, Any]` in framework must be flagged."""
    bad_code = """
from typing import Any

def emit(data: dict[str, Any]) -> None:
    data: dict[str, Any] = {}
"""
    fake_file = ROOT / "src" / "kntgraph" / "concordos" / "_fake_concordo.py"
    rel_path = "src/kntgraph/concordos/_fake_concordo.py"
    tree = ast.parse(bad_code, filename=str(fake_file))
    violations = design_gate.check_ast(fake_file, tree, rel_path)

    wire_violations = [v for v in violations if v.rule_id == "KNT-TYPE-002"]
    assert len(wire_violations) == 2  # one in parameter, one in local variable
    for v in wire_violations:
        assert "dict[str, Any]" in v.message
        assert "JsonValue" in v.remedy


def test_adapter_leakage_prohibited_outside_infra() -> None:
    """KNT-ADAPT-001: Direct third party SDK imports outside infra must be flagged."""
    bad_code = """
import redis
from httpx import AsyncClient

def connect():
    pass
"""
    fake_file = ROOT / "src" / "kntgraph" / "security" / "_fake_sec.py"
    rel_path = "src/kntgraph/security/_fake_sec.py"
    tree = ast.parse(bad_code, filename=str(fake_file))
    violations = design_gate.check_ast(fake_file, tree, rel_path)

    adapt_violations = [v for v in violations if v.rule_id == "KNT-ADAPT-001"]
    assert len(adapt_violations) == 2


def test_adapter_import_under_type_checking_is_allowed() -> None:
    """Under `if TYPE_CHECKING:`, importing third-party types is permitted."""
    allowed_code = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis
    from httpx import AsyncClient
"""
    fake_file = ROOT / "src" / "kntgraph" / "runner" / "_fake_runner.py"
    rel_path = "src/kntgraph/runner/_fake_runner.py"
    tree = ast.parse(allowed_code, filename=str(fake_file))
    violations = design_gate.check_ast(fake_file, tree, rel_path)
    assert not [v for v in violations if v.rule_id == "KNT-ADAPT-001"]


def test_cli_execution_and_format_modes(tmp_path: Path) -> None:
    """Test CLI exit codes and output formatting."""
    violating_file = tmp_path / "bad_framework.py"
    violating_file.write_text(
        "from kntgraph.memory.session import SessionState\n",
        encoding="utf-8",
    )

    # 1. Terminal mode outside src/kntgraph (not in framework)
    res = subprocess.run(
        [sys.executable, str(DESIGN_GATE_PATH), str(violating_file)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0

    # 2. JSON mode on clean repo
    res_json = subprocess.run(
        [sys.executable, str(DESIGN_GATE_PATH), "--format", "json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert res_json.returncode == 0
    data = json.loads(res_json.stdout)
    assert isinstance(data, list)
    assert len(data) == 0
