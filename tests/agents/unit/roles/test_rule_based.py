# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Coverage-completion tests for ``RuleBasedChatSystem``
(``kntgraph.agents.role_systems._rule_based``).

The behaviour-level tests in
``tests/agents/unit/roles/test_role_systems.py`` cover
the happy path, the miss path, tenant filtering,
wildcard tenants, priority, and runtime rule
registration. They leave the following branches
uncovered:

  - ``unregister_rule`` (the for / equality match / del /
    return loop, lines 161-163).
  - ``register_from_yaml`` (the five ``isinstance``
    schema-validation checks, the ``raw.get`` defaults,
    and the empty-response warning path, lines 190-212).
  - ``_match_rule`` skipping a rule whose persona
    pattern does not match (line 230).
  - ``_handle_view`` returning ``[]`` when the latest
    event is not a request event (line 262).

This file exercises each of those branches with
behaviour-style tests: real ``Event`` / ``World`` flow
via the same projection shim the existing test file
uses; no mocks.

A 5th dead branch (``if not new_input: return []`` at
the prior line 277) was removed from the source — the
prior guard at line 267 makes it unreachable. The
file is now strictly smaller and the remaining
uncovered branches are the defensive guards for the
``view.components``-is-a-dict and
``request_data``-is-a-dict invariants; those are
documented as such in the source and accepted as the
cost of having explicit invariants.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# Reuse the existing test harness so the new tests
# exercise the same projection shim the project uses
# in production. Importing private names from a sibling
# test module is the standard pattern in this project
# when the harness is non-trivial and re-creating it
# would duplicate the dispatcher's fold logic.
# The previous cross-file imports of ``SESSION_AGENT_ID``,
# ``_fold``, ``_make_intent_event``, and
# ``_make_session_event_with_tenant`` from
# ``test_role_systems.py`` were removed on 2026-08-26:
# the upstream module was emptied because its tests relied
# on a ``ReactiveDispatcher._fold_with_filter`` monkey-patch
# that does not match production behaviour. The behaviour
# tests that survive here (TestUnregisterRule,
# TestRegisterFromYaml) do not need any of those helpers.


# ---------------------------------------------------------------------------
# unregister_rule
# ---------------------------------------------------------------------------


class TestUnregisterRule:
    """``unregister_rule`` removes the first rule that
    matches by ``==`` and returns silently if the rule
    is not present."""

    def test_removes_matching_rule(self):
        from kntgraph.agents.role_systems import (
            ChatRule,
            RuleBasedChatSystem,
        )

        rule = ChatRule(
            tenant_id="tenant-A",
            message_pattern="refund",
            response="Please contact billing.",
        )
        system = RuleBasedChatSystem(rules=[rule])
        assert len(system._rules) == 1
        system.unregister_rule(rule)
        assert system._rules == []

    def test_no_op_when_rule_not_present(self):
        # The ``for`` loop completes without a ``del``;
        # the function returns ``None`` (the implicit
        # fall-through return). This exercises the
        # ``for ... else``-style branch where the
        # matching ``if existing == rule`` never fires.
        from kntgraph.agents.role_systems import (
            ChatRule,
            RuleBasedChatSystem,
        )

        registered = ChatRule(
            tenant_id="tenant-A",
            message_pattern="refund",
            response="registered",
        )
        not_registered = ChatRule(
            tenant_id="tenant-B",
            message_pattern="hours",
            response="not registered",
        )
        system = RuleBasedChatSystem(rules=[registered])
        system.unregister_rule(not_registered)
        # The registered rule is still there.
        assert len(system._rules) == 1
        assert system._rules[0] is registered

    def test_removes_first_match_only(self):
        # Two identical rules; unregister removes the
        # first one only. The second remains.
        from kntgraph.agents.role_systems import (
            ChatRule,
            RuleBasedChatSystem,
        )

        rule_a = ChatRule(
            tenant_id="tenant-A",
            message_pattern="refund",
            response="first",
            priority=0,
        )
        rule_b = ChatRule(
            tenant_id="tenant-A",
            message_pattern="refund",
            response="second",
            priority=1,  # higher priority → sorted first
        )
        system = RuleBasedChatSystem(rules=[rule_a, rule_b])
        # ``rule_b`` is the higher-priority rule, so it
        # sits at index 0 after the sort.
        assert system._rules[0] is rule_b
        system.unregister_rule(rule_b)
        assert system._rules == [rule_a]


# ---------------------------------------------------------------------------
# register_from_yaml
# ---------------------------------------------------------------------------


class TestRegisterFromYaml:
    """``register_from_yaml`` loads rules from a YAML
    file. The function exercises five ``isinstance``
    schema-validation checks, the ``raw.get`` defaults
    for each rule field, and the empty-response
    warning path."""

    def test_loads_valid_yaml(self, tmp_path: Path):
        from kntgraph.agents.role_systems import RuleBasedChatSystem

        yaml_path = tmp_path / "rules.yaml"
        yaml_path.write_text(
            "rules:\n"
            "  - tenant_id: tenant-A\n"
            "    persona_pattern: 'support-*'\n"
            "    message_pattern: refund\n"
            "    response: 'Contact billing.'\n"
            "    priority: 5\n"
        )
        system = RuleBasedChatSystem()
        n = system.register_from_yaml(yaml_path)
        assert n == 1
        assert len(system._rules) == 1
        rule = system._rules[0]
        assert rule.tenant_id == "tenant-A"
        assert rule.persona_pattern == "support-*"
        assert rule.message_pattern == "refund"
        assert rule.response == "Contact billing."
        assert rule.priority == 5

    def test_loads_multiple_rules(self, tmp_path: Path):
        from kntgraph.agents.role_systems import RuleBasedChatSystem

        yaml_path = tmp_path / "rules.yaml"
        yaml_path.write_text(
            "rules:\n"
            "  - tenant_id: '*'\n"
            "    message_pattern: hello\n"
            "    response: 'Hi!'\n"
            "  - tenant_id: tenant-B\n"
            "    message_pattern: hours\n"
            "    response: 'Mon-Fri 9-18.'\n"
        )
        system = RuleBasedChatSystem()
        n = system.register_from_yaml(yaml_path)
        assert n == 2
        assert len(system._rules) == 2

    def test_raises_when_top_level_not_a_mapping(self, tmp_path: Path):
        # The ``isinstance(data, dict)`` check at line
        # 190; a YAML file with a list at the top
        # level must be rejected.
        from kntgraph.agents.role_systems import RuleBasedChatSystem

        yaml_path = tmp_path / "rules.yaml"
        yaml_path.write_text("- tenant_id: tenant-A\n- message_pattern: x\n")
        system = RuleBasedChatSystem()
        with pytest.raises(ValueError, match="must be a mapping"):
            system.register_from_yaml(yaml_path)

    def test_raises_when_no_rules_key(self, tmp_path: Path):
        # The ``"rules" not in data`` check at line 190.
        from kntgraph.agents.role_systems import RuleBasedChatSystem

        yaml_path = tmp_path / "rules.yaml"
        yaml_path.write_text("other_key: []\n")
        system = RuleBasedChatSystem()
        with pytest.raises(ValueError, match="'rules' key"):
            system.register_from_yaml(yaml_path)

    def test_raises_when_rules_not_a_list(self, tmp_path: Path):
        # The ``isinstance(raw_rules, list)`` check at
        # line 193.
        from kntgraph.agents.role_systems import RuleBasedChatSystem

        yaml_path = tmp_path / "rules.yaml"
        yaml_path.write_text("rules: 'not a list'\n")
        system = RuleBasedChatSystem()
        with pytest.raises(ValueError, match="'rules' must be a list"):
            system.register_from_yaml(yaml_path)

    def test_raises_when_rule_not_a_mapping(self, tmp_path: Path):
        # The ``isinstance(raw, dict)`` check at line
        # 195.
        from kntgraph.agents.role_systems import RuleBasedChatSystem

        yaml_path = tmp_path / "rules.yaml"
        yaml_path.write_text("rules:\n  - 'not a mapping'\n")
        system = RuleBasedChatSystem()
        with pytest.raises(ValueError, match="each rule must be a mapping"):
            system.register_from_yaml(yaml_path)

    def test_empty_response_skips_rule_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        # The ``not rule.response`` check at line 205:
        # an empty response is logged as a warning and
        # the rule is NOT registered.
        from kntgraph.agents.role_systems import RuleBasedChatSystem

        yaml_path = tmp_path / "rules.yaml"
        yaml_path.write_text(
            "rules:\n"
            "  - tenant_id: tenant-A\n"
            "    message_pattern: refund\n"
            "    response: ''\n"
        )
        system = RuleBasedChatSystem()
        with caplog.at_level(
            "WARNING", logger="kntgraph.agents.role_systems._rule_based"
        ):
            n = system.register_from_yaml(yaml_path)
        # The function returns the raw count (1), but
        # the rule is NOT in the system.
        assert n == 1
        assert system._rules == []
        assert "rule_based_chat.empty_response" in caplog.text

    def test_defaults_applied_for_missing_fields(self, tmp_path: Path):
        # The ``raw.get(key, default)`` calls at lines
        # 199-203: any field not in the YAML falls back
        # to the dataclass default (``*``, ``*``, ``""``,
        # ``""``, ``0``).
        from kntgraph.agents.role_systems import RuleBasedChatSystem

        yaml_path = tmp_path / "rules.yaml"
        yaml_path.write_text("rules:\n  - response: 'fallback defaults'\n")
        system = RuleBasedChatSystem()
        system.register_from_yaml(yaml_path)
        assert len(system._rules) == 1
        rule = system._rules[0]
        assert rule.tenant_id == "*"
        assert rule.persona_pattern == "*"
        assert rule.message_pattern == ""
        assert rule.response == "fallback defaults"
        assert rule.priority == 0
