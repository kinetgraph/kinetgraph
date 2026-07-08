# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
``SolutionPromotionBus`` — in-memory FIFO queue.

Carries :class:`SolutionCandidate` instances from the
extractor to the promoter. Mirrors the shape of
``CacheRefreshBus`` (``memory/cache_warmer.py``): a
single producer (``SolutionExtractor``) and a single
consumer (``SolutionPromoter.pump_once``).

The deque is thread/async-safe under cooperative
multitasking; no lock. Multi-consumer setups would swap
the deque for an ``asyncio.Queue``
(interface-compatible).
"""

from __future__ import annotations

import collections

from kntgraph.agents.memory.solutions._values import SolutionCandidate


class SolutionPromotionBus:
    """
    In-memory FIFO queue of `SolutionCandidate`s.

    Mirrors the shape of `CacheRefreshBus`
    (`memory/cache_warmer.py`): a single producer
    (`SolutionExtractor`) and a single consumer
    (`SolutionPromoter.pump_once`). The deque is
    thread/async-safe under cooperative multitasking;
    no lock.

    Multi-consumer setups would swap the deque for an
    `asyncio.Queue` (interface-compatible). The
    Solution tier does not need that today.
    """

    __slots__ = ("_queue",)

    def __init__(self) -> None:
        self._queue: collections.deque[SolutionCandidate] = collections.deque()

    def publish(self, candidate: SolutionCandidate) -> None:
        self._queue.append(candidate)

    def drain(self) -> list[SolutionCandidate]:
        items = list(self._queue)
        self._queue.clear()
        return items

    def __len__(self) -> int:
        return len(self._queue)

    def __repr__(self) -> str:
        return f"SolutionPromotionBus(pending={len(self._queue)})"
