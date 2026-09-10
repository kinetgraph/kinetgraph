# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
concordos.fsm._config -- BusinessFSM configuration (ADR-069 §3.2).

Declares a finite state machine over a ``DomainComponent``:
the states, the allowed transitions (optionally guarded by a
``Specification``), the on-entry events, and the terminal
states. The FSM itself is pure; the config is the only
input that varies per vertical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from kntgraph.core.world.component import DomainComponent
    from kntgraph.concordos.base import Specification

__all__ = ["FSMConfig", "FSMTransition"]


@dataclass(frozen=True, slots=True)
class FSMTransition:
    """
    A single declared transition.

    ``to``    -- target state.
    ``guard`` -- optional Specification evaluated before
                 allowing the transition. If the guard is
                 not satisfied, ``fsm.transition_rejected``
                 is emitted with reason="guard_failed".
    """

    to: str
    guard: "Specification | None" = None


@dataclass(frozen=True, slots=True)
class FSMConfig:
    """
    Declares a finite state machine over a DomainComponent.

    ``component_type`` -- the DomainComponent subclass whose
                          ``state_field`` holds the current state.
    ``state_field``    -- attribute name on the component (str).
    ``transitions``    -- dict[from_state, dict[event_type, FSMTransition]]
    ``on_entry``       -- event_type to emit when entering a state
                          (optional; dict[to_state, event_type]).
    ``terminal``       -- states from which no transition is allowed.
    """

    component_type: type["DomainComponent"]
    state_field: str
    transitions: Mapping[str, Mapping[str, FSMTransition]]
    on_entry: Mapping[str, str] = field(default_factory=dict)
    terminal: frozenset[str] = field(default_factory=frozenset)
