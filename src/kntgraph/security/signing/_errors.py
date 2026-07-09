# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Error types for the signing package.
"""

from __future__ import annotations


class SignatureError(Exception):
    """Base class for signing errors."""


class UnknownAlgorithmError(SignatureError):
    """The signature uses an algorithm we do not support."""

    def __init__(self, alg: str) -> None:
        super().__init__(f"unknown algorithm: {alg!r}")
        self.alg = alg


class CryptoUnavailableError(SignatureError):
    """The crypto extra is not installed; cannot sign or verify."""
