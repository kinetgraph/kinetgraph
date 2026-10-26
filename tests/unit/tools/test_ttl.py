# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Read the request's age from a queued tool request (ADR-085).

A tool's latency budget belongs to the tool. The
``WorkerManager`` holds that declaration, and the reaper — which
runs inside the manager — derives the deadline from it and the
request's own timestamp:

    requested_at + timeout − now

So the reader reads the **timestamp**, not a pre-computed TTL.
Nothing has to be published to anybody, and the router stays
blind to execution time.

## The contract this file pins

The reader receives the **stream entry's field map** — the shape
``XRANGE`` returns: ``{b"payload": <wire json>}``. The wire
payload is the JSON form of a ``tool.<name>.requested`` event,
whose ``timestamp`` records when the request was made.

That is the **only** shape the reader must handle. The reader
is deliberately narrow: a reaper that guesses at payload shapes
is a reaper that can mis-read a deadline. The other shapes in
the test suite are convenience for the caller's own tests, not
part of the production contract.

Decisions the tests make explicit:

1. **The timestamp lives at the event's top level** — it is the
   event's own field, not something the tool's ``data`` carries.
2. **A missing timestamp returns ``None``** — the caller falls
   back to the global ``reaper_idle_time``. The reader never
   invents a value.
3. **A malformed timestamp returns ``None``** — same fallback. A
   reader that raises would take the reaper down; a reader that
   guesses would reclaim a message early.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from kntgraph.tools._ttl import read_requested_at


def _wire(
    *,
    timestamp: object = "2026-10-26T12:00:00+00:00",
    tool: str = "idem",
) -> bytes:
    """A ``tool.<name>.requested`` event in wire form."""
    return json.dumps(
        {
            "event_id": "x",
            "data": {"tool": tool, "params": {}},
            "timestamp": timestamp,
        }
    ).encode()


class TestContract:
    """The production shape: a stream entry's field map."""

    def test_reads_the_timestamp_from_the_entry(self) -> None:
        entry = {b"payload": _wire()}
        assert read_requested_at(entry) == datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)

    def test_reads_the_timestamp_from_the_wire_payload(self) -> None:
        """The caller may pass the decoded payload directly."""
        assert read_requested_at(_wire()) is not None

    def test_reads_from_a_json_string(self) -> None:
        """The payload may arrive already decoded as a string."""
        payload = _wire().decode("utf-8")
        assert read_requested_at(payload) is not None

    def test_reads_from_the_event_envelope(self) -> None:
        """The decoded event (not the field map)."""
        envelope = json.loads(_wire())
        assert read_requested_at(envelope) is not None

    def test_the_timestamp_is_preserved_exactly(self) -> None:
        """No rounding, no truncation — the deadline is derived
        from this value."""
        stamp = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
        entry = {b"payload": _wire(timestamp=stamp.isoformat())}
        assert read_requested_at(entry) == stamp


class TestFallback:
    """A payload with no usable timestamp returns ``None``."""

    def test_missing_timestamp_returns_none(self) -> None:
        assert read_requested_at({b"payload": _wire(timestamp=None)}) is None

    def test_malformed_json_returns_none(self) -> None:
        assert read_requested_at({b"payload": b"{not json"}) is None

    def test_non_dict_payload_returns_none(self) -> None:
        assert read_requested_at({b"payload": b"[1, 2, 3]"}) is None

    def test_invalid_utf8_returns_none(self) -> None:
        assert read_requested_at({b"payload": b"\xff\xfe"}) is None

    def test_a_json_array_is_not_an_envelope(self) -> None:
        """The payload may decode to a list — not an event."""
        assert read_requested_at({b"payload": b"[1, 2, 3]"}) is None

    def test_a_json_scalar_is_not_an_envelope(self) -> None:
        """The payload may decode to a scalar — not an event."""
        assert read_requested_at({b"payload": b'"just a string"'}) is None

    def test_a_json_number_is_not_an_envelope(self) -> None:
        """The payload may decode to a number — not an event."""
        assert read_requested_at({b"payload": b"42"}) is None

    def test_empty_entry_returns_none(self) -> None:
        assert read_requested_at({}) is None

    def test_a_non_string_payload_returns_none(self) -> None:
        """A payload that is neither bytes, str nor mapping is
        unusable."""
        assert read_requested_at(42) is None

    def test_malformed_timestamp_returns_none(self) -> None:
        """A timestamp the reader cannot parse is unusable — the
        caller falls back rather than guessing."""
        assert read_requested_at({b"payload": _wire(timestamp="not-a-date")}) is None


class TestEdgeValues:
    """A timestamp at the epoch and one far in the past both
    parse — the deadline derivation clamps them."""

    def test_epoch_timestamp_is_preserved(self) -> None:
        entry = {b"payload": _wire(timestamp="1970-01-01T00:00:00+00:00")}
        assert read_requested_at(entry) == datetime(1970, 1, 1, tzinfo=UTC)

    def test_a_recent_timestamp_is_preserved(self) -> None:
        stamp = datetime.now(tz=UTC) - timedelta(seconds=42)
        entry = {b"payload": _wire(timestamp=stamp.isoformat())}
        assert read_requested_at(entry) == stamp

    @pytest.mark.parametrize(
        "value",
        ["2026-10-26T12:00:00", "2026-10-26T12:00:00.123456+00:00"],
    )
    def test_timestamp_forms_are_accepted(self, value: str) -> None:
        """The wire form may omit the microseconds or the
        timezone offset; accept both."""
        assert read_requested_at({b"payload": _wire(timestamp=value)}) is not None
