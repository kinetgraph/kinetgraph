# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Redis value codecs.

The redis-py asyncio client returns ``bytes`` for keys and
string values when ``decode_responses=False`` (the framework's
default — the codec lives in one place). The framework's
managers deal exclusively with ``str``; converting at the
boundary is repeated boilerplate at every read site.

This module centralises the bytes→str conversion in three
pure functions:

  - ``decode_value(v)``     : bytes|str|None → str|None
  - ``decode_dict(d)``      : dict[bytes|str, bytes|str]
                              → dict[str, str]
  - ``decode_int_dict(d)``  : dict[bytes|str, bytes|str]
                              → dict[str, int]

``decode_value`` is the only place in the codebase that needs
to know about the ``bytes`` representation. All other call
sites are pure string operations.

The functions are pure (no I/O, no mutation of the input)
and safe to use as the boundary of any Redis read.
"""

from __future__ import annotations

from typing import Optional, TypeVar, Union

T = TypeVar("T")

BytesOrStr = Union[bytes, str, None]


def decode_value(v: BytesOrStr) -> Optional[str]:
    """Coerce a single value to ``str``. ``None`` unchanged; ``bytes`` via UTF-8."""
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8")
    return v


def decode_dict(d: dict) -> dict[str, str]:
    """Coerce dict keys/values from ``bytes`` to ``str``."""
    out: dict[str, str] = {}
    for k, v in d.items():
        ks = decode_value(k)
        vs = decode_value(v)
        if ks is None:
            continue
        if vs is None:
            vs = ""
        out[ks] = vs
    return out


def decode_int_dict(d: dict) -> dict[str, int]:
    """Same as :func:`decode_dict`, but values are coerced to ``int``."""
    out: dict[str, int] = {}
    for k, v in d.items():
        ks = decode_value(k)
        if ks is None:
            continue
        try:
            out[ks] = int(v) if v is not None else 0
        except (TypeError, ValueError):
            out[ks] = 0
    return out


__all__ = ["BytesOrStr", "decode_dict", "decode_int_dict", "decode_value"]
