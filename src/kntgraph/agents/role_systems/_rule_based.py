# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
kntgraph.agents.role_systems._rule_based -- ``RuleBasedChatSystem``.

ADR-049 (Zero Token Architecture support). The
no-LLM path for the chat role: deterministic
responses from a per-tenant rule table. Implements
ZTA principle 4 ("stable → software, uncertain → AI"):

  - When a rule matches the request (tenant_id,
    persona_pattern, message_pattern), the system emits
    ``chat.reply.generated`` directly -- no
    ``tool.chat_llm.requested``, no LLM call.
  - When no rule matches, the system does nothing; a
    downstream ``ChatRoleSystem`` (or any other
    LLM-backed system registered after it in the
    dispatcher list) handles the fallback.

Rule table
----------

A :class:`ChatRule` is a 4-tuple:

  - ``tenant_id``: the tenant scope (``"*"`` = any
    tenant).
  - ``persona_pattern``: a ``fnmatch``-style glob (e.g.
    ``"support-*"``).
  - ``message_pattern``: a substring match (case
    insensitive); first match wins (by ``priority``,
    then registration order).
  - ``response``: the deterministic reply to emit.

In production, rules are typically loaded at boot
from a YAML/JSON file:

    rules:
      - tenant_id: "*"
        persona_pattern: "*"
        message_pattern: "refund"
        response: "Please contact billing@example.com."
      - tenant_id: "tenant-A"
        persona_pattern: "support-*"
        message_pattern: "hours"
        response: "Mon-Fri, 9-18 UTC."

Operators can register rules programmatically
(:meth:`RuleBasedChatSystem.register_rule`) or from a
YAML file (:meth:`RuleBasedChatSystem.register_from_yaml`).

Composability
-------------

The system is designed to be **stacked before** the
canonical ``ChatRoleSystem`` in the dispatcher's
system list:

    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[
            RuleBasedChatSystem(rules=[...]),  # fast path
            ChatRoleSystem(persona="..."),      # LLM fallback
        ],
        ...
    )

Rules that match short-circuit the LLM call. Rules
that miss delegate to the next system.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from kntgraph.core.components.memory import SessionComponent
from kntgraph.core.event import Event
from kntgraph.core.world import AgentView, World

from ._base import _BaseRoleSystem, _emit_chat_completion
from ._prompts import ChatReply


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChatRule:
    """
    A deterministic rule for the chat path.

    ``tenant_id="*"`` matches any tenant. ``priority``
    is used to break ties when multiple rules match a
    single request (higher wins).
    """

    tenant_id: str = "*"
    persona_pattern: str = "*"
    message_pattern: str = ""
    response: str = ""
    priority: int = 0


class RuleBasedChatSystem(_BaseRoleSystem):
    """
    No-LLM chat path: deterministic responses from a
    per-tenant rule table.

    Falls back to the canonical LLM path (the next
    system in the dispatcher's list handles the
    request) when no rule matches. This composability
    is the canonical expression of ZTA principle 4
    ("hybrid: stable → software, uncertain → AI").

    Reacts to the same event types as
    :class:`ChatRoleSystem` (``user.intent``). On a
    matching rule, emits ``chat.reply.generated``
    directly without the ``tool.chat_llm.requested``
    step.

    The rule table is in-memory; :meth:`register_from_yaml`
    is a convenience for loading from disk. The
    example ``09b_solution_lookup_zta`` ships a sample
    YAML.
    """

    REQUEST_EVENT_TYPE = "user.intent"
    GENERATED_EVENT_TYPE = "chat.reply.generated"
    OUTPUT_MODEL = ChatReply

    def __init__(
        self,
        *,
        rules: Optional[list[ChatRule]] = None,
        case_insensitive_message: bool = True,
        persona: str = "",
    ) -> None:
        super().__init__()
        # Defensive copy; sorted by descending priority
        # then by registration order (stable sort).
        self._rules: list[ChatRule] = sorted(
            list(rules or []),
            key=lambda r: (-r.priority,),
        )
        self._case_insensitive = case_insensitive_message
        # The persona this rule system substitutes for.
        # ``RuleBasedChatSystem`` is the deterministic
        # short-circuit that runs **before** the LLM-backed
        # ``ChatRoleSystem`` in the dispatcher stack; the
        # caller passes the persona so rule matching can
        # use the same persona the downstream LLM system
        # would have used. ``""`` keeps the default
        # permissive (matches only ``"*"`` patterns),
        # which preserves the historical behaviour for
        # callers that have not migrated.
        self._persona = persona

    def register_rule(self, rule: ChatRule) -> None:
        """Insert a rule. Maintains the priority-sorted
        invariant."""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: (-r.priority,))

    def unregister_rule(self, rule: ChatRule) -> None:
        """Remove the first matching rule (by ``==``)."""
        for i, existing in enumerate(self._rules):
            if existing == rule:
                del self._rules[i]
                return

    def rules_for_tenant(self, tenant_id: str) -> list[ChatRule]:
        """Return all rules that target this tenant
        (``tenant_id == "*"`` is included)."""
        return [
            r for r in self._rules if r.tenant_id == "*" or r.tenant_id == tenant_id
        ]

    def register_from_yaml(self, path: str | Path) -> int:
        """Load rules from a YAML file. Returns the
        number of rules registered.

        Expected schema::

            rules:
              - tenant_id: tenant-A
                persona_pattern: support-*
                message_pattern: refund
                response: "Please contact billing."
                priority: 0  # optional

        Unknown keys are ignored (forward-compat).
        """
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or "rules" not in data:
            raise ValueError(f"rule file {path!r} must be a mapping with a 'rules' key")
        raw_rules = data["rules"]
        if not isinstance(raw_rules, list):
            raise ValueError(f"rule file {path!r}: 'rules' must be a list")
        for raw in raw_rules:
            if not isinstance(raw, dict):
                raise ValueError(f"rule file {path!r}: each rule must be a mapping")
            rule = ChatRule(
                tenant_id=str(raw.get("tenant_id", "*")),
                persona_pattern=str(raw.get("persona_pattern", "*")),
                message_pattern=str(raw.get("message_pattern", "")),
                response=str(raw.get("response", "")),
                priority=int(raw.get("priority", 0)),
            )
            if not rule.response:
                logger.warning(
                    "rule_based_chat.empty_response",
                    extra={"tenant_id": rule.tenant_id},
                )
                continue
            self.register_rule(rule)
        return len(raw_rules)

    def _match_rule(
        self,
        *,
        tenant_id: str,
        persona: str,
        message: str,
    ) -> Optional[ChatRule]:
        """Find the first rule (by priority) that matches
        the request. ``tenant_id == "*"`` matches any
        tenant; ``persona_pattern`` is ``fnmatch``;
        ``message_pattern`` is a substring match (case
        insensitive by default)."""
        candidate_message = message.lower() if self._case_insensitive else message
        for rule in self._rules:
            if rule.tenant_id != "*" and rule.tenant_id != tenant_id:
                continue
            if not fnmatch.fnmatchcase(persona, rule.persona_pattern):
                continue
            needle = (
                rule.message_pattern.lower()
                if self._case_insensitive
                else rule.message_pattern
            )
            if needle and needle not in candidate_message:
                continue
            return rule
        return None

    def __call__(self, world: World) -> list[Event]:
        """
        Walk every agent's ``user.intent`` slot and
        emit a deterministic completion for any
        matching rule.

        On miss, return ``[]`` (the next system in the
        dispatcher's list handles the LLM fallback).
        """
        out: list[Event] = []
        for agent_id, view in world.views.items():
            events = self._handle_view(view)
            if events:
                out.extend(events)
        return out

    def _handle_view(self, view: AgentView) -> list[Event]:
        """Single-view handling. Mirrors
        :meth:`ChatRoleSystem._handle_view` (the
        pattern the base system follows)."""
        if not self._is_request_event(view, view.last_event_id):
            return []
        if not isinstance(view.components, dict):
            return []
        request_data = view.components.get(self.REQUEST_EVENT_TYPE)
        if not isinstance(request_data, dict):
            return []
        # Read the new input + persona + tenant_id
        # from the view. The tenant_id is part of
        # the ``SessionComponent`` if a session is
        # loaded; otherwise we fall back to the
        # agent_id (operators may use the agent_id
        # as the tenant scope in single-tenant
        # deployments).
        new_input = self._read_new_input(view)
        # ``_read_new_input`` only returns ``""`` for
        # non-dict ``request_data``; the guard at line
        # 267 above already returns early in that case,
        # so by the time we reach here ``new_input`` is
        # always non-empty (a string extracted from the
        # dict, or ``str(data)`` for a non-empty dict).
        # No second guard needed — the prior guard
        # establishes the invariant.
        session = view.components.get(SessionComponent)
        tenant_id = (
            getattr(session, "tenant_id", None) if session is not None else None
        ) or view.agent_id
        persona = self._persona_for_view(view)
        # Persona is read from the base class via a
        # subclass hook (``_persona_for_view``); default
        # is the empty string, matching
        # ``ChatRoleSystem(persona="")``.
        rule = self._match_rule(tenant_id=tenant_id, persona=persona, message=new_input)
        if rule is None:
            # Miss: let the next system handle the
            # fallback (canonical LLM path).
            return []
        # Build the completion and emit it. We use
        # the same ``_emit_chat_completion`` helper
        # the base role system uses so the wire
        # format is identical to the LLM path.
        completion = _emit_chat_completion(
            base=self,
            view=view,
            text=rule.response,
            follow_up_questions=[],
            input_text=new_input,
        )
        logger.info(
            "rule_based_chat.hit",
            extra={
                "agent_id": view.agent_id,
                "tenant_id": tenant_id,
                "rule_tenant_id": rule.tenant_id,
                "persona_pattern": rule.persona_pattern,
                "message_pattern": rule.message_pattern,
            },
        )
        return [completion]

    def _persona_for_view(self, view: AgentView) -> str:
        """Read the persona from the instance attribute.

        The persona is constructor-set (``RuleBasedChatSystem(persona=...)``)
        and matches the persona of the downstream
        ``ChatRoleSystem(persona=...)`` this rule system
        is short-circuiting. Callers pass the same
        persona to both systems so a rule's
        ``persona_pattern`` glob is evaluated against
        the real persona — fixing the v0.9.0 stub
        that always returned ``""`` and made every
        non-``"*"`` pattern a dead branch.
        """
        return self._persona


# The base ``_BaseRoleSystem`` lives in
# ``__init__.py``. Importing the helper from there
# keeps the wire format consistent across rule
# systems and LLM systems.
