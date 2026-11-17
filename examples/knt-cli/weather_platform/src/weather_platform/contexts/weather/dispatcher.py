# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

from typing import Any

from kntgraph.runner import ReactiveDispatcher
from kntgraph.security.principal import Principal, PrincipalLevel
from kntgraph.stream.event_log import EventLog
from kntgraph.tools.router import ToolRouter

from .agents.weather_agent import WEATHER_AGENT
from .tools.open_meteo_api import OpenMeteoApi


def build_weather_dispatcher(
    log: EventLog, redis: Any, tool_router: ToolRouter
) -> ReactiveDispatcher:
    """Build the weather context's dispatcher.

    The agent is **declared**, not wired: the framework derives
    the runtime from ``WEATHER_AGENT`` and ``install()``
    registers it here (ADR-088). The dispatcher never learns
    what an agent is -- it only receives systems and projections.
    """
    dispatcher = ReactiveDispatcher(
        log=log,
        systems=[],
        redis=redis,
        tool_router=tool_router,
        poll_interval=0.5,
    )

    # The ``Principal`` is the runtime credential the agent acts
    # under -- it is never stored in a declaration (ADR-088
    # §2.4).
    WEATHER_AGENT.install(
        dispatcher,
        principal=Principal.from_agent_id(
            "weather.weather",
            level=PrincipalLevel.agent,
            key_id="local-dev",
        ),
    )

    return dispatcher


def get_weather_tools() -> list:
    """The context's tools -- registered with the worker manager."""
    return [OpenMeteoApi()]
