# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

import importlib
import os
import sys
from pathlib import Path

from typer.testing import CliRunner

from kntgraph.cli.main import app

runner = CliRunner()


def _scaffold(tmp_path: Path) -> Path:
    """Initialise a project with one bounded context."""
    os.chdir(tmp_path)
    init_result = runner.invoke(app, ["init", "project", "my_app"])
    assert init_result.exit_code == 0
    project = tmp_path / "my_app"
    os.chdir(project)
    assert runner.invoke(app, ["new", "context", "sales"]).exit_code == 0
    return project


def test_knt_new_agent(tmp_path: Path):
    current_dir = os.getcwd()
    try:
        project = _scaffold(tmp_path)

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

        # 5. The declaration is the agent (ADR-088): one frozen
        # dataclass naming the persona, the capability policy,
        # the role event, the two execution cycles and the
        # domain types.
        content = expected_file.read_text()
        assert "from kntgraph.agents import AgentDefinition, RoleSpec" in content
        assert "CHECKOUT_AGENT = AgentDefinition(" in content
        assert 'name="checkout"' in content
        assert 'persona="checkout"' in content
        assert 'role_event_type="role.assigned"' in content
        # The historical wiring helpers are gone: the runtime
        # is derived from the declaration, not hand-wired.
        assert "get_checkout_agent_systems" not in content
        assert "get_checkout_agent_tools" not in content
        assert "get_checkout_agent_allowed_events" not in content
        # The agent file does NOT import or instantiate the
        # ``ReactiveDispatcher`` (the dispatcher is wired in
        # ``<context>/dispatcher.py``, not in the agent file).
        # The word may appear in the docstring, but the
        # **import** must not be present.
        assert "from kntgraph.runner import ReactiveDispatcher" not in content
        assert "ReactiveDispatcher(" not in content
        assert "ReactiveDispatcher =" not in content

        assert project.is_dir()
    finally:
        os.chdir(current_dir)


def test_knt_new_agent_derives_the_declaration_from_the_context(tmp_path: Path):
    """The declaration is derived from the context's artifacts.

    The tool allow-list is the context's tools, the domain
    types are its components, and the two execution cycles are
    its Concordos (ADR-088).
    """
    current_dir = os.getcwd()
    try:
        _scaffold(tmp_path)

        assert runner.invoke(app, ["new", "component", "sales.Invoice"]).exit_code == 0
        assert (
            runner.invoke(app, ["new", "tool", "sales.ProcessPayment"]).exit_code == 0
        )
        assert (
            runner.invoke(
                app,
                [
                    "concordo",
                    "add",
                    "fsm",
                    "sales.InvoiceFSM",
                    "--component",
                    "Invoice",
                ],
            ).exit_code
            == 0
        )
        assert (
            runner.invoke(
                app,
                [
                    "concordo",
                    "new",
                    "saga",
                    "sales.InvoiceFlow",
                    "--steps",
                    "validate:check,emit:send",
                ],
            ).exit_code
            == 0
        )
        result = runner.invoke(app, ["new", "agent", "sales.CheckoutAgent"])
        assert result.exit_code == 0

        content = Path("src/my_app/contexts/sales/agents/checkout_agent.py").read_text()

        # The capability policy is the context's tools.
        assert 'allowed_tools=("process_payment",)' in content
        # The domain types are the context's components.
        assert "domain_types=(Invoice,)" in content
        # The two execution cycles are the context's Concordos.
        assert "fsm=InvoiceFSMConcordo" in content
        assert "sagas=(InvoiceFlowConcordo,)" in content
        # The concordos are imported from the context.
        assert (
            "from ..concordos.invoice_fsm_concordo import InvoiceFSMConcordo" in content
        )
        assert (
            "from ..concordos.invoice_flow_concordo import InvoiceFlowConcordo"
            in content
        )
    finally:
        os.chdir(current_dir)


def test_knt_new_agent_installs_the_declaration_in_the_dispatcher(
    tmp_path: Path,
):
    """The dispatcher installs the declaration with a principal.

    The declaration holds no runtime credential; the host
    supplies it at install time (ADR-088 §2.4).
    """
    current_dir = os.getcwd()
    try:
        _scaffold(tmp_path)

        assert (
            runner.invoke(app, ["new", "tool", "sales.ProcessPayment"]).exit_code == 0
        )
        assert (
            runner.invoke(app, ["new", "agent", "sales.CheckoutAgent"]).exit_code == 0
        )

        dispatcher = Path("src/my_app/contexts/sales/dispatcher.py").read_text()
        assert "from .agents.checkout_agent import CHECKOUT_AGENT" in dispatcher
        assert "CHECKOUT_AGENT.install(" in dispatcher
        assert "principal=Principal.from_agent_id(" in dispatcher
        # The tool is registered with the worker manager.
        assert (
            "worker_manager.register(ProcessPayment(), acl=default_acl())" in dispatcher
        )
    finally:
        os.chdir(current_dir)


def test_knt_new_agent_leaves_a_hand_edited_dispatcher_alone(tmp_path: Path):
    """A derived file the user edited is never clobbered.

    The refresh rewrites a derived file only when it is still
    the template's untouched output.
    """
    current_dir = os.getcwd()
    try:
        _scaffold(tmp_path)

        dispatcher = Path("src/my_app/contexts/sales/dispatcher.py")
        dispatcher.write_text(dispatcher.read_text() + "\n# hand written\n")

        result = runner.invoke(app, ["new", "tool", "sales.ProcessPayment"])
        assert result.exit_code == 0
        assert "was edited by hand" in result.stdout
        assert "# hand written" in dispatcher.read_text()
    finally:
        os.chdir(current_dir)


def test_generated_declaration_imports_and_derives_the_runtime(tmp_path: Path):
    """The scaffolded project must import and derive.

    The generated files are not decoration: the declaration
    imports, the bundle derives, and the projected role carries
    the declared capability policy (ADR-088 §2.6).
    """
    current_dir = os.getcwd()
    try:
        project = _scaffold(tmp_path)

        assert runner.invoke(app, ["new", "component", "sales.Invoice"]).exit_code == 0
        assert (
            runner.invoke(app, ["new", "tool", "sales.ProcessPayment"]).exit_code == 0
        )
        assert (
            runner.invoke(
                app,
                [
                    "concordo",
                    "add",
                    "fsm",
                    "sales.InvoiceFSM",
                    "--component",
                    "Invoice",
                ],
            ).exit_code
            == 0
        )
        assert (
            runner.invoke(app, ["new", "agent", "sales.CheckoutAgent"]).exit_code == 0
        )

        src = str(project / "src")
        sys.path.insert(0, src)
        try:
            module = importlib.import_module(
                "my_app.contexts.sales.agents.checkout_agent"
            )
            definition = module.CHECKOUT_AGENT

            assert definition.name == "checkout"
            assert definition.persona.allowed_tools == ("process_payment",)

            bundle = definition.bundle()
            assert bundle.name == "agent:checkout"
            assert [type(s).__name__ for s in bundle.systems] == ["FSMSystem"]
            assert [type(p).__name__ for p in bundle.projections] == [
                "FSMProjection",
                "RoleProjection",
            ]
        finally:
            sys.path.remove(src)
            for name in list(sys.modules):
                if name.startswith("my_app"):
                    del sys.modules[name]
    finally:
        os.chdir(current_dir)
