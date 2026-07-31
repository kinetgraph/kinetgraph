# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Memory-tier sub-config (mixin).

Holds TTL knobs for the three short-term memory
managers (ADR-014 §2.1) and the Solution cache
(ADR-010 / ADR-049). Each default reflects the
lifecycle of the data:

  - Session: short-lived (24h) — the conversation
    state is rebuilt on every new session anyway.
  - Profile: stable PME config (no TTL) — the data
    outlives any single session and is updated by
    explicit ``profile.updated`` events.
  - Continuity: sliding 90 days — recent usage
    patterns. Renewed on every ``record_*`` write.
  - Solution: no TTL — Solutions are explicitly
    promoted / invalidated by the operator; an
    operator-set TTL would surprise the audit trail.

Operators can override per-deployment. ``None``
(or 0) means "no TTL" — the key persists until
explicitly deleted.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from kntgraph.infra.config._base import BaseSettings


class MemorySettingsMixin(BaseSettings):
    """TTL knobs for Session, Profile, Continuity, and Solution."""

    session_ttl_seconds: int = Field(default=24 * 60 * 60)
    profile_ttl_seconds: Optional[int] = Field(default=None)
    continuity_ttl_seconds: int = Field(default=90 * 24 * 60 * 60)
    solution_ttl_seconds: Optional[int] = Field(default=None)
