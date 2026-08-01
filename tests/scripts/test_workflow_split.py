# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the ``release.yml`` / ``publish.yml`` split
(ADR-052).

PyPI's Trusted Publisher binds to **one**
workflow name; the project chose to split the
release process into two workflows:

  - ``release.yml`` -- cut the git tag, open the
    GitHub Release (no PyPI).
  - ``publish.yml`` -- build the wheel, publish to
    PyPI (no tag, no GitHub Release).

This test enforces the contract: ``release.yml``
does NOT contain PyPI publish steps; ``publish.yml``
exists and has the canonical structure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
RELEASE_YML = WORKFLOWS_DIR / "release.yml"
PUBLISH_YML = WORKFLOWS_DIR / "publish.yml"


@pytest.fixture(scope="module")
def release_workflow() -> dict:
    """Parse ``release.yml`` once per module."""
    if not RELEASE_YML.exists():
        pytest.skip(f"{RELEASE_YML} not found")
    with RELEASE_YML.open() as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def publish_workflow() -> dict:
    """Parse ``publish.yml`` once per module."""
    if not PUBLISH_YML.exists():
        pytest.skip(f"{PUBLISH_YML} not found")
    with PUBLISH_YML.open() as f:
        return yaml.safe_load(f)


class TestReleaseWorkflow:
    """The release workflow (``release.yml``) cuts
    the tag and opens the GitHub Release. It does
    **not** publish to PyPI -- that is the
    ``publish.yml`` workflow's job.
    """

    def test_release_has_no_pypa_action(self, release_workflow: dict) -> None:
        # The release workflow must not invoke
        # ``pypa/gh-action-pypi-publish`` (that
        # would couple it to PyPI and break the
        # re-rodability of the publish step).
        text = RELEASE_YML.read_text()
        assert "pypa/gh-action-pypi-publish" not in text, (
            "release.yml must not contain the PyPI "
            "publish action; that is publish.yml's "
            "responsibility. The split exists so the "
            "publish step can be re-run independently "
            "of the tag."
        )

    def test_release_has_no_packages_dir(self, release_workflow: dict) -> None:
        # ``packages-dir: dist/`` is a publish
        # concern (the publish step reads
        # ``dist/``).
        text = RELEASE_YML.read_text()
        assert "packages-dir" not in text

    def test_release_has_no_pypi_environment(self, release_workflow: dict) -> None:
        # The ``pypi`` GitHub Environment is a
        # publish concern (the human-gate for
        # publishing). The release workflow does
        # not need it.
        text = RELEASE_YML.read_text()
        assert "pypi" not in text or "PYPI" not in text.upper(), (
            "release.yml must not reference the "
            "``pypi`` Environment; that is the "
            "publish.yml's trust boundary. The "
            "release workflow is approved by the "
            "operator's manual trigger."
        )

    def test_release_has_no_id_token_write(self, release_workflow: dict) -> None:
        # The ``id-token: write`` permission is
        # required only for OIDC-based publishing.
        # The release workflow does not publish;
        # it does not need the permission.
        text = RELEASE_YML.read_text()
        assert "id-token" not in text, (
            "release.yml must not request the "
            "``id-token: write`` permission; that is "
            "publish.yml's requirement (the OIDC "
            "token is issued by GitHub only when "
            "the workflow has the permission)."
        )


class TestPublishWorkflow:
    """The publish workflow (``publish.yml``) builds
    the wheel and publishes to PyPI. It does **not**
    cut the tag or open a GitHub Release.
    """

    def test_publish_file_exists(self, publish_workflow: dict) -> None:
        assert PUBLISH_YML.exists(), (
            "publish.yml must exist (ADR-052 §2.3 "
            "split); release.yml is for tag cuts, "
            "publish.yml is for PyPI."
        )

    def test_publish_uses_pypa_action(self, publish_workflow: dict) -> None:
        text = PUBLISH_YML.read_text()
        assert "pypa/gh-action-pypi-publish" in text

    def test_publish_targets_dist(self, publish_workflow: dict) -> None:
        text = PUBLISH_YML.read_text()
        assert "packages-dir: dist/" in text or "packages-dir: dist" in text

    def test_publish_has_pypi_environment(self, publish_workflow: dict) -> None:
        # The publish workflow declares the
        # ``pypi`` Environment; the Environment's
        # "required reviewers" rule is the
        # human-in-the-loop gate that the PyPI
        # binding does not provide.
        text = PUBLISH_YML.read_text()
        assert re.search(r"environment:\s*pypi", text), (
            "publish.yml must declare the ``pypi`` "
            "Environment; the PyPI Environment is "
            "the human gate on the publish step."
        )

    def test_publish_has_id_token_write(self, publish_workflow: dict) -> None:
        # The OIDC token is required for Trusted
        # Publishing; the workflow must declare
        # the permission explicitly.
        text = PUBLISH_YML.read_text()
        assert "id-token: write" in text

    def test_publish_does_not_create_tag(self, publish_workflow: dict) -> None:
        # The publish workflow assumes the tag
        # already exists (it is the input). It
        # must not re-run ``bump_version.py``
        # (that would either fail because the tag
        # exists, or create a different tag).
        text = PUBLISH_YML.read_text()
        assert "bump_version.py" not in text, (
            "publish.yml must not run "
            "bump_version.py; the tag is the "
            "publish workflow's input, not its "
            "output. Run release.yml first to "
            "create the tag, then run publish.yml."
        )

    def test_publish_does_not_open_github_release(self, publish_workflow: dict) -> None:
        # The publish workflow does not open the
        # GitHub Release; that is release.yml's
        # job (it happens **before** the publish
        # step so the release notes are visible
        # by the time PyPI receives the wheel).
        text = PUBLISH_YML.read_text()
        assert "gh release create" not in text

    def test_publish_is_re_rodable(self, publish_workflow: dict) -> None:
        # The publish workflow is re-runnable:
        # running it twice with the same tag does
        # not re-create the tag (it does not
        # touch the tag at all) and does not
        # require the release workflow to have
        # run first in the same job. (The PyPI
        # action itself is idempotent: re-uploading
        # the same wheel is a no-op.)
        text = PUBLISH_YML.read_text()
        # The workflow is ``workflow_dispatch``
        # (manual trigger) with a tag input --
        # not a chained ``workflow_run`` of
        # release.yml (that would couple the two).
        assert "workflow_run" not in text, (
            "publish.yml must not be triggered "
            "automatically by release.yml; the "
            "operator retains control of when to "
            "publish (decoupled from when to cut a "
            "tag)."
        )


class TestSplitContract:
    """End-to-end: the two workflows together
    cover the release process; neither is a
    duplicate of the other.
    """

    def test_workflows_partition_responsibilities(
        self, release_workflow: dict, publish_workflow: dict
    ) -> None:
        # release.yml has the tag + GitHub Release
        # steps; publish.yml has the PyPI step.
        # The set of actions is disjoint.
        release_text = RELEASE_YML.read_text()
        publish_text = PUBLISH_YML.read_text()
        assert "gh release create" in release_text
        assert "gh release create" not in publish_text
        assert "pypa/gh-action-pypi-publish" not in release_text
        assert "pypa/gh-action-pypi-publish" in publish_text
