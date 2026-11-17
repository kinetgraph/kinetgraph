# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""The weather agent -- declared as data (ADR-088).

An agent is one ``AgentDefinition``: its persona, its capability
policy, its state machine and its workflows. The framework
derives the installable runtime from this declaration
(``definition.bundle()`` yields the systems and the projections
the dispatcher registers), so nothing here is hand-wired.

The declaration holds no runtime handle and no derived state.
The ``Principal`` that authorises the agent's outbound requests
is supplied by the host at install time.
"""

from kntgraph.agents import AgentDefinition, RoleSpec

from ..components.location_intent import LocationIntent


WEATHER_AGENT = AgentDefinition(
    name="weather",
    persona=RoleSpec(
        persona="weather",
        # The capability policy (ADR-088 §3.5): the names of the
        # tools this agent may request. It is projected into
        # ``RoleComponent.allowed_tools``, which is what gate 2 of
        # the three-gate model reads. An empty allow-list forbids
        # every tool -- the role is installed, so the permissive
        # fallback does not apply.
        allowed_tools=("open_meteo_api",),
    ),
    # The event type that assigns the role at runtime. The
    # framework's ``RoleProjection`` reads the persona and the
    # instructions from the event payload; the capability policy
    # always comes from the spec above.
    role_event_type="role.assigned",
    # The reactive cycle -- zero-token governance. ``None`` means
    # the agent declares no state machine.
    fsm=None,
    # The deliberative cycle -- tool orchestration with
    # compensation.
    sagas=(),
    # The domain components the agent's rules read, addressed by
    # their concrete class.
    domain_types=(LocationIntent,),
)
