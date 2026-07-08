# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the single-source-of-truth of memory-agent
prefixes (code review item #12).

Before this fix, the prefixes ``"session:"`` and
``"profile:"`` lived in five places:

  - ``consolidation.py`` (private constants in parse_agent_id)
  - ``session.py`` (in agent_id_for)
  - ``profile.py`` (in agent_id_for)
  - ``session.py`` and ``profile.py`` (Redis cache_key
    prefixes, a separate concern)

If a developer renamed the agent_id convention for
sessions (e.g. ``"sess:"``), they would have to remember
to update the parser in consolidation.py, or the
Consolidator/Projector would silently misclassify every
memory agent.

After this fix, the prefix lives in ONE place: the
manager class exposes ``agent_id_prefix`` (a class
attribute), and the parser / MemoryAgent consult that
class attribute. Renaming requires updating exactly one
file.

The tests below pin the contract that:
  - Each BaseShortTermMemory subclass has
    ``agent_id_prefix``.
  - ``parse_agent_id`` correctly classifies an agent_id
    even if the prefix is a non-default value (proves the
    parser is consulting the manager, not a hard-coded
    string).
  - ``MemoryAgent.agent_id`` matches what the manager's
    own ``agent_id_for`` produces.
"""

from __future__ import annotations


from kntgraph.memory.consolidation import (
    MemoryAgent,
    parse_agent_id,
)
from kntgraph.memory.continuity import ContinuityManager
from kntgraph.memory.profile import ProfileManager
from kntgraph.memory.session import SessionManager


# ---------------------------------------------------------------------------
# Class-level prefix constants
# ---------------------------------------------------------------------------


class TestAgentIdPrefixOnManager:
    def test_session_manager_exposes_agent_id_prefix(self):
        """
        The session manager must expose its agent_id
        prefix as a class attribute. The parser uses this
        to classify an EventLog agent_id.
        """
        assert hasattr(SessionManager, "agent_id_prefix")
        assert isinstance(SessionManager.agent_id_prefix, str)
        assert SessionManager.agent_id_prefix.endswith(":")

    def test_profile_manager_exposes_agent_id_prefix(self):
        assert hasattr(ProfileManager, "agent_id_prefix")
        assert isinstance(ProfileManager.agent_id_prefix, str)
        assert ProfileManager.agent_id_prefix.endswith(":")

    def test_continuity_manager_exposes_agent_id_prefix(self):
        assert hasattr(ContinuityManager, "agent_id_prefix")
        assert isinstance(ContinuityManager.agent_id_prefix, str)
        assert ContinuityManager.agent_id_prefix.endswith(":")

    def test_prefixes_are_distinct(self):
        """The three prefixes must not collide."""
        assert SessionManager.agent_id_prefix != ProfileManager.agent_id_prefix
        assert SessionManager.agent_id_prefix != ContinuityManager.agent_id_prefix
        assert ProfileManager.agent_id_prefix != ContinuityManager.agent_id_prefix


# ---------------------------------------------------------------------------
# parse_agent_id consults the manager, not a local constant
# ---------------------------------------------------------------------------


class TestParserUsesManagerPrefix:
    def test_session_agent_round_trip(self):
        """
        An agent_id built via SessionManager.agent_id_for
        must be classified as a session by parse_agent_id.
        """
        agent_id = SessionManager.agent_id_for("s-1")
        result = parse_agent_id(agent_id)
        assert result is not None
        assert result.kind == "session"
        assert result.id1 == "s-1"

    def test_profile_agent_round_trip(self):
        agent_id = ProfileManager.agent_id_for("tenant-x", "user-y")
        result = parse_agent_id(agent_id)
        assert result is not None
        assert result.kind == "profile"
        assert result.id1 == "tenant-x"
        assert result.id2 == "user-y"

    def test_unknown_prefix_returns_none(self, clean_redis):
        """
        A string that does NOT start with any known
        manager prefix is not memory. The parser must
        return None.
        """
        assert parse_agent_id("fechamento:tenant-x:2026-01") is None
        assert parse_agent_id("NF-001") is None
        assert parse_agent_id("agent.spawned") is None

    def test_empty_string_returns_none(self):
        assert parse_agent_id("") is None


# ---------------------------------------------------------------------------
# MemoryAgent.agent_id matches SessionManager.agent_id_for
# ---------------------------------------------------------------------------


class TestMemoryAgentDelegation:
    def test_session_memory_agent_agent_id_matches_manager(self):
        """
        ``MemoryAgent.session("s-1").agent_id`` must equal
        ``SessionManager.agent_id_for("s-1")``. If the
        MemoryAgent hard-codes the prefix, the two could
        drift apart.
        """
        m = MemoryAgent.session("s-1")
        assert m.agent_id == SessionManager.agent_id_for("s-1")

    def test_profile_memory_agent_agent_id_matches_manager(self):
        m = MemoryAgent.profile("tenant-x", "user-y")
        assert m.agent_id == ProfileManager.agent_id_for("tenant-x", "user-y")

    def test_continuity_memory_agent_agent_id_matches_manager(self):
        m = MemoryAgent.continuity("tenant-x", "user-y")
        assert m.agent_id == ContinuityManager.agent_id_for("tenant-x", "user-y")

    def test_continuity_round_trip(self):
        agent_id = ContinuityManager.agent_id_for("tenant-x", "user-y")
        result = parse_agent_id(agent_id)
        assert result is not None
        assert result.kind == "continuity"
        assert result.id1 == "tenant-x"
        assert result.id2 == "user-y"


# ---------------------------------------------------------------------------
# Renaming: if the prefix changes, the parser follows
# ---------------------------------------------------------------------------


class TestParserTracksPrefixChange:
    """
    The single-source-of-truth property: if a developer
    changes the prefix in one place, the parser must
    follow.

    We patch the class attribute to a different value
    and verify the parser now classifies strings with the
    new prefix as memory and rejects the old prefix.
    """

    def test_session_prefix_change_propagates(self, monkeypatch):
        """
        Patch SessionManager.agent_id_prefix to a
        non-default value. An agent_id built with the
        NEW prefix must be classified as a session by
        parse_agent_id.
        """
        monkeypatch.setattr(SessionManager, "agent_id_prefix", "sess:")
        agent_id = "sess:new-id"
        result = parse_agent_id(agent_id)
        assert result is not None
        assert result.kind == "session"
        assert result.id1 == "new-id"

    def test_old_session_prefix_no_longer_parsed(self, monkeypatch):
        """
        After renaming the prefix, the OLD prefix must
        no longer be classified as memory.
        """
        monkeypatch.setattr(SessionManager, "agent_id_prefix", "sess:")
        # ``session:old-id`` is no longer a memory agent
        # (under the new convention). It is also not a
        # profile (which has its own prefix).
        assert parse_agent_id("session:old-id") is None

    def test_profile_prefix_change_propagates(self, monkeypatch):
        monkeypatch.setattr(ProfileManager, "agent_id_prefix", "prof:")
        agent_id = "prof:tenant-x:user-y"
        result = parse_agent_id(agent_id)
        assert result is not None
        assert result.kind == "profile"
        assert result.id1 == "tenant-x"
        assert result.id2 == "user-y"
