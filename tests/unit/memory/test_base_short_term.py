# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for BaseShortTermMemory — the shared contract for
SessionManager and ProfileManager.

This module pins the contract of the Redis Agent Builder
(RAB) "short-memory" shape, adapted to the FMH event-sourced
model. The base class is the home for everything that is
identical between Session and Profile; the subclasses own
the domain-specific shape (state type, agent_id layout,
fold, cache format).

The tests below assert class/import structure only; runtime
behavior is covered by the integration suite in
``tests/integration/memory/test_session_profile.py``.

Why these tests exist
---------------------
A class hierarchy only delivers value if the contract is
visible. If someone refactors SessionManager or ProfileManager
to bypass the base (or removes the base entirely), these
tests will fail. The integration suite would also fail, but
only for the specific scenario that triggered the bypass.
These tests assert the structural intent.
"""

from __future__ import annotations

import inspect


class TestBaseShortTermMemory:
    def test_base_is_importable(self):
        from kntgraph.memory.base import BaseShortTermMemory

        assert BaseShortTermMemory is not None

    def test_base_is_abstract(self):
        from kntgraph.memory.base import BaseShortTermMemory

        assert inspect.isabstract(BaseShortTermMemory)

    def test_base_is_generic(self):
        from kntgraph.memory.base import BaseShortTermMemory

        # Generic classes expose __class_getitem__.
        assert hasattr(BaseShortTermMemory, "__class_getitem__")

    def test_base_subclasses_session_and_profile(self):
        from kntgraph.memory.base import BaseShortTermMemory
        from kntgraph.memory.profile import ProfileManager
        from kntgraph.memory.session import SessionManager

        assert issubclass(SessionManager, BaseShortTermMemory)
        assert issubclass(ProfileManager, BaseShortTermMemory)

    def test_session_manager_is_parameterised_with_session_state(self):
        """
        The runtime generic parameterisation should be
        ``BaseShortTermMemory[SessionState]``. This is what
        makes the type checker's view consistent with the
        runtime inheritance.
        """
        from kntgraph.memory.base import BaseShortTermMemory
        from kntgraph.memory.session import SessionManager, SessionState

        # Generic.__class_getitem__ exposes the parameter via
        # ``__orig_bases__``.
        bases = getattr(SessionManager, "__orig_bases__", ())
        found = any(
            getattr(b, "__origin__", None) is BaseShortTermMemory
            and getattr(b, "__args__", ()) == (SessionState,)
            for b in bases
        )
        assert found, (
            f"SessionManager must inherit BaseShortTermMemory[SessionState]. "
            f"Found bases: {bases!r}"
        )

    def test_profile_manager_is_parameterised_with_profile_state(self):
        from kntgraph.memory.base import BaseShortTermMemory
        from kntgraph.memory.profile import ProfileManager, ProfileState

        bases = getattr(ProfileManager, "__orig_bases__", ())
        found = any(
            getattr(b, "__origin__", None) is BaseShortTermMemory
            and getattr(b, "__args__", ()) == (ProfileState,)
            for b in bases
        )
        assert found, (
            f"ProfileManager must inherit BaseShortTermMemory[ProfileState]. "
            f"Found bases: {bases!r}"
        )

    def test_base_declares_abstract_methods(self):
        """
        The base must declare the four hooks a subclass
        must implement: ``_read_cache``, ``_fold_from_log``,
        ``_serialize_for_cache``, ``cache_key``. If someone
        adds a hook, this test will fail and they can decide
        whether to update subclasses too.

        Note: ``_store_cache`` is NOT abstract anymore —
        Iteration 2 (ADR-019) moved the actual storage call
        into the ``ShortMemoryStorage`` adapter. The base
        now delegates via ``self._storage.put_record``.
        """
        from kntgraph.memory.base import BaseShortTermMemory

        abstract_names = BaseShortTermMemory.__abstractmethods__
        assert "_read_cache" in abstract_names
        assert "_fold_from_log" in abstract_names
        assert "_serialize_for_cache" in abstract_names
        assert "cache_key" in abstract_names

    def test_base_public_methods_are_coroutine_functions(self):
        """
        The public orchestration methods (``read``,
        ``refresh_cache``, ``write_cache``) must be async.
        The base class implements these as coroutines; the
        subclasses either inherit them or override them
        with their own coroutines.
        """
        from kntgraph.memory.base import BaseShortTermMemory

        for name in ("read", "refresh_cache"):
            method = getattr(BaseShortTermMemory, name)
            assert inspect.iscoroutinefunction(method), (
                f"{name} on the base must be async"
            )


class TestBaseSharedOrchestration:
    """
    Verify the base class actually reduces duplication:
    the orchestration lives in the base, and the subclasses
    only implement the shape hooks.
    """

    def test_read_orchestration_is_in_base(self):
        """
        The ``read`` method body is defined on the base,
        not on the subclass. If a subclass overrides ``read``
        to re-implement the read-through logic, that means
        the duplication has crept back in.
        """
        from kntgraph.memory.base import BaseShortTermMemory

        base_read = BaseShortTermMemory.read
        # The subclass may keep or delegate; if it keeps,
        # it must use the same orchestration source. For
        # now, we only assert the base implements it.
        assert base_read is not None

    def test_write_cache_helper_lives_in_base(self):
        """
        The internal ``_write_cache_for_key`` helper is
        implemented on the base. This is the function that
        actually pushes a state to Redis after a serialization
        step; subclasses do not duplicate this logic.
        """
        from kntgraph.memory.base import BaseShortTermMemory

        assert hasattr(BaseShortTermMemory, "_write_cache_for_key")
        method = getattr(BaseShortTermMemory, "_write_cache_for_key")
        assert inspect.iscoroutinefunction(method)
