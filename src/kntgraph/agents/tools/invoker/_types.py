# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Internal signal types for the ToolInvoker package.
"""

from __future__ import annotations


class ArgsInvalid(Exception):
    """
    Internal signal raised by `_resolve_args` when the
    merged args do not validate against the Tool's
    `input_schema`. Caught by `handle_request_event`
    and converted to a `tool.{name}.args_invalid`
    event for the DLQ.

    The structured fields mirror `SchemaValidationError`
    so the consumer sees the same detail in the
    emitted event.
    """

    def __init__(
        self,
        message: str,
        *,
        missing: list[str],
        type_mismatches: list[tuple[str, str, str]],
        unexpected: list[str],
    ) -> None:
        super().__init__(message)
        self.missing = missing
        self.type_mismatches = type_mismatches
        self.unexpected = unexpected
