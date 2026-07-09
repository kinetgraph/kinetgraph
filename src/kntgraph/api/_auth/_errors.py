# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
api._auth._errors -- authentication error types.

``AuthError`` is the single error type raised by
:class:`RedisAPIKeyVerifier` when a request cannot be
authenticated. The router converts it into 401 (missing
key) or 403 (key present but invalid/revoked) based on
the ``kind`` attribute.

This module is a private implementation detail of
``_auth``; the public surface is unchanged.
"""

from __future__ import annotations


class AuthError(Exception):
    """
    Raised when the request cannot be authenticated.

    The router converts this into 401 (missing key) or
    403 (key present but invalid/revoked) based on the
    `kind` attribute.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


__all__ = ["AuthError"]
