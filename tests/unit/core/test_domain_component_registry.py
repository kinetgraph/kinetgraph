# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the :mod:`kntgraph.core.world.component`
collision-detection contract.

The ``@domain_component`` decorator writes to a process-wide
:class:`ClassVar` dict on :class:`DomainComponent`. Two test files
that register different classes for the same ``event_type``
would silently overwrite each other and the dispatcher fold
would hydrate the wrong class for every subsequent event of
that type (the test pollution bug fixed by the collision
check). These tests pin the three documented cases of the
decorator:

  - First registration: registers normally.
  - Idempotent re-registration (same class): no-op.
  - Conflicting re-registration (different class):
    ``ValueError`` at import time.
"""

from __future__ import annotations

import pytest
from dataclasses import dataclass

from kntgraph.core.world.component import (
    DomainComponent,
    domain_component,
    reset_domain_registry,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    """Run every test with a clean registry.

    The registry is process-wide; without this fixture, a
    test that registers ``"order.submit"`` would leak into
    the next test and pollute the assertion in the third
    case (``ValueError``).
    """
    reset_domain_registry()
    yield
    reset_domain_registry()


def test_first_registration_registers_normally() -> None:
    """A fresh event_type registers without side effects."""

    @domain_component("test.first")
    @dataclass(frozen=True, slots=True)
    class First(DomainComponent):
        pass

    assert DomainComponent.__domain_registry__["test.first"] is First


def test_idempotent_re_registration_is_a_noop() -> None:
    """Re-applying the decorator to the SAME class does not
    raise — the registry uses ``is`` identity, so re-registering
    the same class is a no-op. (Modules can be re-imported
    under ``importlib.reload`` and the class object stays the
    same.)"""

    @domain_component("test.idempotent")
    @dataclass(frozen=True, slots=True)
    class Idempotent(DomainComponent):
        pass

    # Second application with the SAME class object — must
    # not raise and must not change the registry.
    decorator = domain_component("test.idempotent")
    decorator(Idempotent)  # idempotent, no exception

    assert DomainComponent.__domain_registry__["test.idempotent"] is Idempotent


def test_conflicting_registration_raises_value_error() -> None:
    """Two different classes registering the same event_type
    raises ``ValueError`` at the second import — the operator
    sees the conflict at import time, not at runtime.

    The first class registers via the decorator (the typical
    ``@domain_component`` usage). The second class attempts
    to register the same event_type via the explicit
    decorator application (this is the path that runs at
    import time when two modules both use the decorator)."""

    @domain_component("test.collision")
    @dataclass(frozen=True, slots=True)
    class First(DomainComponent):
        pass

    with pytest.raises(ValueError) as exc_info:
        # The second class does NOT use the @decorator syntax
        # (otherwise the exception fires at module-import
        # time, BEFORE pytest can capture it). Instead, we
        # simulate the collision: another class is decorated
        # with the SAME event_type by calling the decorator
        # factory's wrapper explicitly.
        @dataclass(frozen=True, slots=True)
        class Second(DomainComponent):
            pass

        domain_component("test.collision")(Second)

    message = str(exc_info.value)
    assert "test.collision" in message
    assert "First" in message
    assert "Second" in message
    assert "refusing to overwrite" in message


def test_first_registration_survives_failed_conflict() -> None:
    """A conflicting re-registration must NOT overwrite the
    first class — the original registration remains
    reachable."""

    @domain_component("test.preserve")
    @dataclass(frozen=True, slots=True)
    class Original(DomainComponent):
        pass

    with pytest.raises(ValueError):
        @domain_component("test.preserve")
        @dataclass(frozen=True, slots=True)
        class Other(DomainComponent):
            pass

    assert DomainComponent.__domain_registry__["test.preserve"] is Original


def test_reset_domain_registry_clears_everything() -> None:
    """``reset_domain_registry`` empties the dict so the
    next decorator application is treated as a fresh
    registration."""

    @domain_component("test.reset")
    @dataclass(frozen=True, slots=True)
    class Before(DomainComponent):
        pass

    assert "test.reset" in DomainComponent.__domain_registry__

    reset_domain_registry()

    assert "test.reset" not in DomainComponent.__domain_registry__

    @domain_component("test.reset")
    @dataclass(frozen=True, slots=True)
    class After(DomainComponent):
        pass

    assert DomainComponent.__domain_registry__["test.reset"] is After
