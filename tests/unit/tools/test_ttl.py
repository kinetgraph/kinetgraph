# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Read the per-message TTL from a queued tool request (ADR-085).

The dispatcher injects ``ttl_remaining`` into the request's
``data`` before routing it to the tool queue. The reaper reads
it back to decide **per message** when the message may be
reclaimed.

## The contract this file pins

The reader receives the **stream entry's field map** — the shape
``XRANGE`` returns: ``{b"payload": <wire json>}``. The wire
payload is the JSON form of a ``tool.<name>.requested`` event,
whose ``data`` carries the TTL.

That is the **only** shape the reader must handle. The reader
is deliberately narrow: a reaper that guesses at payload shapes
is a reaper that can mis-read a deadline. The other shapes in
the test suite are convenience for the caller's own tests, not
part of the production contract.

Decisions the tests make explicit:

1. **The TTL lives in the event's ``data``**, next to the tool's
   parameters — not at the event's top level. It is the
   dispatcher's ``ToolCallTTL`` territory, and ``data`` is
   where the request's parameters already live.
2. **A missing TTL returns ``None``** — the caller falls back to
   the global ``reaper_idle_time``. The reader never invents a
   value.
3. **A malformed TTL returns ``None``** — same fallback. A
   reader that raises would take the reaper down; a reader that
   guesses would reclaim a message early.
4. **Zero and negative TTLs are preserved.** They mean
   "the deadline has passed"; the caller clamps to zero
   (reclaimable immediately). Clamping here would lose the
   distinction between "no TTL" and "expired TTL".
"""

from __future__ import annotations

import json

import pytest

from kntgraph.tools._ttl import TTL_REMAINING_KEY, read_ttl_remaining


def _wire(*, ttl: object = None, tool: str = "idem") -> bytes:
    """A ``tool.<name>.requested`` event in wire form."""
    data: dict[str, object] = {"tool": tool, "params": {}}
    if ttl is not None:
        data[TTL_REMAINING_KEY] = ttl
    return json.dumps({"event_id": "x", "data": data}).encode()


class TestContract:
    """The production shape: a stream entry's field map."""

    def test_reads_the_ttl_from_the_entry(self) -> None:
        entry = {b"payload": _wire(ttl=900)}
        assert read_ttl_remaining(entry) == 900.0

    def test_reads_the_ttl_from_the_wire_payload(self) -> None:
        """The caller may pass the decoded payload directly."""
        assert read_ttl_remaining(_wire(ttl=900)) == 900.0

    def test_reads_from_the_event_envelope(self) -> None:
        """The decoded event (not the field map)."""
        envelope = json.loads(_wire(ttl=42))
        assert read_ttl_remaining(envelope) == 42.0

    def test_reads_from_a_bare_data_mapping(self) -> None:
        """The decoded ``data`` mapping."""
        assert read_ttl_remaining({TTL_REMAINING_KEY: 7}) == 7.0


class TestFallback:
    """A payload with no usable TTL returns ``None``."""

    def test_missing_ttl_returns_none(self) -> None:
        assert read_ttl_remaining({b"payload": _wire()}) is None

    def test_missing_ttl_in_data_returns_none(self) -> None:
        assert read_ttl_remaining({TTL_REMAINING_KEY: None}) is None

    def test_malformed_json_returns_none(self) -> None:
        assert read_ttl_remaining({b"payload": b"{not json"}) is None

    def test_non_numeric_ttl_returns_none(self) -> None:
        assert read_ttl_remaining({b"payload": _wire(ttl="abc")}) is None

    def test_non_dict_payload_returns_none(self) -> None:
        assert read_ttl_remaining({b"payload": b"[1, 2, 3]"}) is None

    def test_invalid_utf8_returns_none(self) -> None:
        assert read_ttl_remaining({b"payload": b"\xff\xfe"}) is None

    def test_empty_entry_returns_none(self) -> None:
        assert read_ttl_remaining({}) is None


class TestEdgeValues:
    """Zero and negative TTLs mean "expired" and must survive."""

    def test_zero_ttl_is_preserved(self) -> None:
        assert read_ttl_remaining({b"payload": _wire(ttl=0)}) == 0.0

    def test_negative_ttl_is_preserved(self) -> None:
        assert read_ttl_remaining({b"payload": _wire(ttl=-5)}) == -5.0

    def test_float_ttl_is_preserved(self) -> None:
        assert read_ttl_remaining({b"payload": _wire(ttl=12.5)}) == 12.5

    @pytest.mark.parametrize("value", [0, -1, 900])
    def test_numeric_strings_are_accepted(self, value: int) -> None:
        """The wire form of a float may serialise as a string
        depending on the producer; accept it."""
        assert read_ttl_remaining({b"payload": _wire(ttl=str(value))}) == float(value)
