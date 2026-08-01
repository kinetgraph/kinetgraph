# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
check_version.py -- fail CI when the installed version
disagrees with the latest git tag (ADR-051).

``setuptools_scm`` derives the version at install time
from the latest annotated git tag matching the project's
``vX.Y.Z`` scheme. When the local checkout has commits
past the latest tag, the installed ``__version__`` is
the ``devN+g<sha>`` form (e.g. ``0.10.1.dev0+gc0adc4211``);
the script accepts that as "in sync" (the working tree
is correctly ahead of the tag, and the ``+g<sha>`` proves
the install was rebuilt after the commits).

The script fails when the installed version is **older**
than the latest tag, which is the case when:

  - The install cache is stale (``uv sync`` not
    re-run after the tag was pushed).
  - The tag was created retroactively (PR 1 of
    ADR-051) and the developer did not re-sync.

Remediation in both cases: ``uv sync``.
"""

from __future__ import annotations

import subprocess
import sys

from packaging.version import InvalidVersion, Version


def _get_installed_version() -> Version:
    """Return the installed ``kntgraph.__version__``
    parsed as a PEP 440 ``Version``.

    The ``"+unknown"`` fallback (no ``_version.py``)
    is treated as a configuration error: if the
    install is missing the version file, the build
    is not configured correctly, and the script
    should fail.
    """
    try:
        import kntgraph
    except ImportError as e:
        raise SystemExit(
            f"check_version: cannot import kntgraph ({e}); the install is broken."
        ) from e
    raw = getattr(kntgraph, "__version__", None)
    if raw is None:
        raise SystemExit(
            "check_version: kntgraph.__version__ is not "
            "set; the install is missing _version.py."
        )
    if raw == "0.0.0+unknown":
        raise SystemExit(
            "check_version: kntgraph.__version__ is "
            "'0.0.0+unknown' (no _version.py generated); "
            "run 'uv sync' to generate it from the git tag."
        )
    try:
        return Version(raw)
    except InvalidVersion as e:
        raise SystemExit(
            f"check_version: kntgraph.__version__={raw!r} is not PEP 440 compliant: {e}"
        ) from e


def _get_latest_tag() -> str:
    """Return the latest annotated git tag, stripped
    of the leading ``v``. Fails with a non-zero
    exit if no tag exists (a configuration error:
    the project must always have at least one tag).
    """
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            "check_version: 'git describe --tags "
            "--abbrev=0' failed; the repository has "
            "no annotated tags. ADR-051 requires at "
            "least one tag (vX.Y.Z) for the version "
            "to be derivable.\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout.strip().lstrip("v")


def main() -> int:
    """Compare the installed version to the latest
    tag and fail with a diagnostic if the installed
    version is **behind** the tag.

    The "in sync" condition is permissive:

      - The installed version's **base** may equal
        the tag (the working tree is at the tag).
      - The installed version's base may be a
        forward bump of the tag (e.g. ``0.10.1``
        when the tag is ``0.10.0``). This is the
        normal state when there are commits past
        the tag (``setuptools_scm`` infers
        ``0.10.1.dev0`` for the first commit past
        ``0.10.0``).
      - The installed version may carry a
        ``devN+g<sha>`` suffix (commits past the
        tag, work in progress).

    The drift condition is strict: the installed
    version is **older** than the tag. That
    happens when the install cache is stale
    (``uv sync`` not re-run after the tag was
    pushed) or when the tag was created
    retroactively (PR 1 of ADR-051) and the
    developer did not re-sync.

    Comparison ignores the ``+g<sha>`` local
    segment (which ``packaging.version`` parses
    as a local-version label) and the ``devN``
    pre-release segment (which sorts before the
    release; ``Version("0.10.0") > Version("0.10.0.dev0")``).
    """
    installed = _get_installed_version()
    try:
        tag = Version(_get_latest_tag())
    except InvalidVersion as e:
        raise SystemExit(
            f"check_version: latest tag is not a valid PEP 440 version: {e}"
        ) from e
    # The installed version's **base** must be >=
    # the tag. We compare the bases (drop the
    # devN suffix) so that ``0.10.1.dev0`` is
    # treated as "at least 0.10.1", not "before
    # 0.10.1".
    installed_base = Version(installed.base_version)
    if installed_base < tag:
        raise SystemExit(
            f"check_version: version drift: "
            f"installed={installed_base} (dev={installed.dev}) "
            f"but tag={tag}; the install is older "
            f"than the latest tag. Run 'uv sync' to "
            f"refresh."
        )
    print(
        f"check_version: OK — "
        f"installed={installed_base} "
        f"(dev={installed.dev}) "
        f"is at or past tag=v{tag}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
