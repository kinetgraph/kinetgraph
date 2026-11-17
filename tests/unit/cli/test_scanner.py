# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the CLI's context scanner (the derived agent model).

The scanner reads the **AST** of a bounded context's artifacts.
These tests pin the two properties the derivation depends on:

  1. It finds what the context declares -- the tools, the
     components, the Concordos and the agents.
  2. It follows the **code**, not the filename: a renamed class
     or a renamed tool is found under its new name, and a file
     that does not parse is skipped instead of breaking the
     scan of the rest of the context.
"""

from __future__ import annotations

import os
from pathlib import Path

from kntgraph.cli._scanner import scan_context

CONTEXT = "sales"


def _write(path: Path, content: str) -> None:
    """Write a file, creating its parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _context_dir(tmp_path: Path) -> Path:
    """The context's directory inside a fake project."""
    return tmp_path / "src" / "my_app" / "contexts" / CONTEXT


def test_scan_context_reads_the_declared_artifacts(tmp_path: Path) -> None:
    """The scanner finds every kind of artifact."""
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        base = _context_dir(tmp_path)

        _write(
            base / "tools" / "process_payment.py",
            "from kntgraph.tools.worker import tool_worker\n"
            "\n"
            "\n"
            '@tool_worker(name="process_payment")\n'
            "class ProcessPayment:\n"
            "    async def invoke(self, *, idempotency_key: str) -> dict:\n"
            "        return {}\n",
        )
        _write(
            base / "components" / "invoice.py",
            "from dataclasses import dataclass\n"
            "\n"
            "from kntgraph.core.world import DomainComponent\n"
            "\n"
            "\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class Invoice(DomainComponent):\n"
            '    status: str = "draft"\n',
        )
        _write(
            base / "concordos" / "invoice_fsm_concordo.py",
            "from kntgraph.concordos.fsm import BusinessFSMConcordo\n"
            "\n"
            "\n"
            "InvoiceFSMConcordo = BusinessFSMConcordo(config)\n",
        )
        _write(
            base / "agents" / "checkout_agent.py",
            "from kntgraph.agents import AgentDefinition\n"
            "\n"
            "\n"
            'CHECKOUT_AGENT = AgentDefinition(name="checkout")\n',
        )

        inventory = scan_context("my_app", CONTEXT)

        assert [t.tool_name for t in inventory.tools] == ["process_payment"]
        assert [t.class_name for t in inventory.tools] == ["ProcessPayment"]
        assert [c.class_name for c in inventory.components] == ["Invoice"]
        assert inventory.fsm() is not None
        assert inventory.fsm().class_name == "InvoiceFSMConcordo"
        assert inventory.sagas() == ()
        assert [a.constant for a in inventory.agents] == ["CHECKOUT_AGENT"]
        assert [a.agent_name for a in inventory.agents] == ["checkout"]
    finally:
        os.chdir(current_dir)


def test_scan_context_follows_the_code_not_the_filename(tmp_path: Path) -> None:
    """A renamed class is found under its new name.

    The filename of a Concordo cannot be reversed into the
    class name -- the CamelCase of ``invoice_fsm_concordo.py``
    is not ``InvoiceFSMConcordo``. The scanner reads the code.
    """
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        base = _context_dir(tmp_path)

        _write(
            base / "concordos" / "invoice_fsm_concordo.py",
            "from kntgraph.concordos.fsm import BusinessFSMConcordo\n"
            "\n"
            "\n"
            "InvoiceLifecycle = BusinessFSMConcordo(config)\n",
        )
        _write(
            base / "tools" / "charge.py",
            "from kntgraph.tools.worker import tool_worker\n"
            "\n"
            "\n"
            '@tool_worker(name="charge_customer")\n'
            "class Charge:\n"
            "    async def invoke(self, *, idempotency_key: str) -> dict:\n"
            "        return {}\n",
        )

        inventory = scan_context("my_app", CONTEXT)

        assert inventory.fsm() is not None
        assert inventory.fsm().class_name == "InvoiceLifecycle"
        # The tool's registered name is the decorator's
        # ``name=``, not the file stem.
        assert [t.tool_name for t in inventory.tools] == ["charge_customer"]
        assert [t.class_name for t in inventory.tools] == ["Charge"]
    finally:
        os.chdir(current_dir)


def test_scan_context_skips_a_file_that_does_not_parse(tmp_path: Path) -> None:
    """One broken file must not break the scan of the context.

    A half-written artifact is a normal state of a working
    tree; the scanner skips it and reports the rest.
    """
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        base = _context_dir(tmp_path)

        _write(base / "tools" / "broken.py", "def invoke(:\n")
        _write(
            base / "tools" / "process_payment.py",
            "from kntgraph.tools.worker import tool_worker\n"
            "\n"
            "\n"
            '@tool_worker(name="process_payment")\n'
            "class ProcessPayment:\n"
            "    async def invoke(self, *, idempotency_key: str) -> dict:\n"
            "        return {}\n",
        )

        inventory = scan_context("my_app", CONTEXT)

        assert [t.class_name for t in inventory.tools] == ["ProcessPayment"]
    finally:
        os.chdir(current_dir)


def test_scan_context_of_an_absent_context_is_empty(tmp_path: Path) -> None:
    """A context that was never scaffolded is not an error."""
    current_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        inventory = scan_context("my_app", "never_scaffolded")
        assert inventory.tools == ()
        assert inventory.components == ()
        assert inventory.concordos == ()
        assert inventory.agents == ()
        assert inventory.fsm() is None
    finally:
        os.chdir(current_dir)
