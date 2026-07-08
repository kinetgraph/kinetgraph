# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
intent_router.helpers -- Pure helpers for the HTTP gateway.

Stateless utilities:

  - ``_sanitize_idempotency_key(raw)``: validate and
    normalise the ``Idempotency-Key`` header value
    before it flows into the event_id hash. Returns
    the sanitised key (empty string for ``None`` /
    empty / whitespace-only) or raises ``ValueError``
    on invalid input. The caller converts ``ValueError``
    into the transport-specific exception
    (``HTTPException(400)`` in the FastAPI path).

  - ``_deterministic_event_id(agent_id, type_, target,
    args, idempotency_key)``: build a UUID5 from the
    request fields. The hash inputs are sorted /
    deterministic: the same tuple always produces
    the same ``event_id``. This is the foundation of
    HTTP retry safety: the EventLog dedupes on
    ``event_id``, so a retry that arrives after the
    first request is appended simply no-ops.

  - The module-level constants:
    ``_INTENT_NS`` (UUID5 namespace), ``_MAX_IDEMPOTENCY_KEY_LEN``
    (128 chars), and ``_IDEMPOTENCY_KEY_BAD_CHARS``
    (regex of disallowed control chars).

Why a separate module?

The helpers are pure: they import nothing from FastAPI
or Redis, and have no dependency on the surrounding
class / factory. Splitting them lets the test in
``test_idempotency_key.py`` exercise the
sanitisation without standing up a FastAPI app.

The ``_INTENT_NS`` namespace is stable across
processes — clients can recompute the same UUID5 to
verify that their request produced the expected
``event_id``.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional


# UUID5 namespace for intent event_ids. Stable across
# processes; clients can recompute it for verification.
_INTENT_NS = uuid.UUID("f5e9c5b1-2c4a-4d3e-8a7f-1b2c3d4e5f60")

# Idempotency-Key trust boundary.
#
# The header is folded into the deterministic event_id
# hash (see `_deterministic_event_id`). An attacker
# that controls the header can:
#   1. Cause a DoS by submitting an arbitrarily large
#      key (each append flows through json.dumps and
#      uuid5 — both bounded by input size).
#   2. Inject CRLF into log lines that include the key
#      (any logger.error(...) that echoes the raw value
#      would split the log entry).
#   3. Force collision with another tenant's event_id
#      (a client that wants to bypass idempotency
#      dedup could send a key chosen to match another
#      request — but the dedup is bounded by the
#      full payload, not the key alone, so the impact
#      is limited).
#
# The rules below are deliberately conservative:
_MAX_IDEMPOTENCY_KEY_LEN = 128
# Disallow control chars (including CR, LF, NUL, TAB,
# VT, FF) and non-printable Unicode. Spaces and most
# punctuation are allowed because clients commonly use
# short semantic keys ("create-order-42").
_IDEMPOTENCY_KEY_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028-\u202f]")


def _sanitize_idempotency_key(raw: Optional[str]) -> str:
    """
    Validate and normalise the ``Idempotency-Key``
    header value before it flows into the event_id
    hash.

    Returns the sanitised key (empty string for
    ``None``/empty/whitespace-only) or raises
    ``ValueError`` on invalid input. The caller is
    responsible for converting ``ValueError`` into the
    transport-specific exception (``HTTPException(400)``
    in the FastAPI path).

    Rationale for the ValueError contract: the helper
    is module-scope and must NOT import FastAPI (which
    would make the module uncollectable in environments
    that have not installed the ``[api]`` extra). The
    conversion to ``HTTPException`` happens at the call
    site, where FastAPI is already imported.

    Rules:
      - ``None`` or empty/whitespace → return ``""``
        (treated as "no idempotency key" — the same
        default as before the validation existed).
      - Non-string → ``ValueError``.
      - Length > ``_MAX_IDEMPOTENCY_KEY_LEN`` (128)
        → ``ValueError``.
      - Contains a control char (CR, LF, NUL, TAB, etc.)
        → ``ValueError``. We do NOT silently strip: a
        key with CR/LF is almost certainly either
        malicious or a bug on the client, and silently
        changing it would mean the server-side event_id
        no longer matches what the client computes.
      - Otherwise → return the value unchanged.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"Idempotency-Key must be a string, got {type(raw).__name__}")
    if not raw or not raw.strip():
        return ""
    if len(raw) > _MAX_IDEMPOTENCY_KEY_LEN:
        raise ValueError(
            f"Idempotency-Key too long "
            f"({len(raw)} chars; "
            f"max {_MAX_IDEMPOTENCY_KEY_LEN})"
        )
    if _IDEMPOTENCY_KEY_BAD_CHARS.search(raw):
        raise ValueError(
            "Idempotency-Key contains control "
            "characters (CR/LF/NUL/TAB/etc.); "
            "remove them and retry"
        )
    return raw


def _deterministic_event_id(
    *,
    agent_id: str,
    type_: str,
    target: str,
    args: dict[str, Any],
    idempotency_key: str,
) -> str:
    """
    Build a UUID5 from the request fields.

    The hash inputs are sorted/deterministic: the same
    `(agent_id, type, target, args, idempotency_key)`
    tuple always produces the same `event_id`. This is
    the foundation of HTTP retry safety: the EventLog
    dedupes on `event_id`, so a retry that arrives after
    the first request is appended simply no-ops.

    ``args`` is typed ``dict[str, Any]`` (not
    ``Mapping[str, JsonValue]``) because the caller is
    a Pydantic model whose OpenAPI schema needs a
    permissive ``additionalProperties`` to keep the
    generated client SDK clean. The runtime invariant
    — every leaf is a JSON-serialisable value — is
    enforced by ``IntentRequest._validate_args_json_value``
    in :mod:`kntgraph.api.schemas` (see also the
    ``json.dumps`` call below, which would raise on any
    non-serialisable value before the UUID5 is computed).
    """
    payload = {
        "agent_id": agent_id,
        "type": type_,
        "target": target,
        "args": args,
        "idempotency_key": idempotency_key,
    }
    serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(_INTENT_NS, serialised))


__all__ = [
    "_IDEMPOTENCY_KEY_BAD_CHARS",
    "_INTENT_NS",
    "_MAX_IDEMPOTENCY_KEY_LEN",
    "_deterministic_event_id",
    "_sanitize_idempotency_key",
]
