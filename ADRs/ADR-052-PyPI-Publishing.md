<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-052: PyPI publishing via Trusted Publishing

**Status:** Proposed
**Date:** 2026-07-31
**Version:** 0.1.0
**Authors:** kntgraph architecture team
**Related to:** [ADR-051](./ADR-051-Release-Versioning-via-Git-Tags.md) (the version is the git tag; this ADR publishes it to PyPI), [PEP 740](https://peps.python.org/pep-0740/) (Trusted Publishing, the modern OIDC-based mechanism), [pypa/gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish) (the canonical action this ADR wraps), [PyPI Package Name Reservation](https://pypi.org/help/#package-name) (the ``kntgraph`` name is currently unregistered as of 2026-07-31)

> **First publish, not retroactive.** This ADR
> cuts a release and publishes it to PyPI in the
> same step. There is no "publish the historical
> v0.7.0 - v0.10.0" plan: the next release (the
> first PyPI release) is v0.11.0 or v1.0.0,
> whichever the operator decides when this ADR is
> Accepted.

## 1. Context

### 1.1 The current state (post-ADR-051)

kntgraph has a working release process (ADR-051):

- The git tag ``vX.Y.Z`` is the version.
- ``setuptools_scm`` derives ``__version__`` at
  install time.
- The release workflow
  (``.github/workflows/release.yml``) bumps
  locally, runs ``changelog_release.py``, and
  pushes the tag.
- The README has a version badge that
  ``update_version_badge.py`` keeps in sync with
  ``__version__``.

What is **not** working:

- **No PyPI package.** An adopter today has to
  install via ``git+https://...`` or
  copy-paste. There is no ``pip install kntgraph``
  in the world.
- **No version discovery on PyPI.** Adopters
  cannot ``pip show kntgraph`` to get the
  version; they have to know the repo URL and
  clone to find out.
- **No dependency resolution.** A consumer
  package that wants to use ``kntgraph`` in a
  modern ``pyproject.toml`` has no
  ``kntgraph>=0.10`` to depend on.

The project has crossed the line where PyPI
publishing is a one-day, well-understood job.
The foundation (ADR-051) covers 80% of the work.
This ADR covers the remaining 20%.

### 1.2 Why **Trusted Publishing** (PEP 740)

The traditional PyPI publish flow is:

1. The maintainer creates a PyPI account.
2. The maintainer generates an API token.
3. The maintainer stores the token in a
   password manager.
4. The maintainer copies the token into a GitHub
   secret (or a ``.pypirc``).
5. The CI workflow reads the secret and
   authenticates as the maintainer.

This has multiple failure modes:

- **Token leakage** (the secret is leaked; the
  attacker can push malicious releases).
- **Token rotation** (every 6 months; manual).
- **Multi-maintainer** (every maintainer needs
  their own token; the security is per-person).
- **Post-mortem** (which token leaked? who had
  access?).

PEP 740 (Trusted Publishing) replaces the API
token with an **OIDC token** issued by the CI
provider. The flow becomes:

1. The maintainer registers the GitHub repo on
   PyPI as a Trusted Publisher.
2. The CI workflow requests an OIDC token from
   GitHub Actions (the standard
   ``permissions: id-token: write`` + the
   ``pypa/gh-action-pypi-publish`` action).
3. PyPI verifies the OIDC token (it is signed
   by GitHub; the claim is "this job is running
   in repo X, ref Y, workflow Z, environment
   W").
4. The release ships.

The security properties:

- **No long-lived secrets** to leak. The OIDC
  token is short-lived (a few minutes) and tied
  to a specific workflow run.
- **PyPI enforces the binding**: only jobs from
  the registered repo + workflow + environment
  can publish. A leaked secret from another
  project cannot impersonate this one.
- **Multi-maintainer is free**: anyone who can
  merge to main can trigger the publish workflow
  (the OIDC token carries the GitHub identity,
  not a per-person secret).
- **Auditability**: PyPI's release history shows
  the GitHub workflow + commit that produced
  each release.

This is the modern standard (PyPI recommends it
in the [official docs](https://docs.pypi.org/trusted-publishers/));
the API token flow is documented as "legacy".

## 2. Decision

**Adopt Trusted Publishing for kntgraph.** The
release workflow (``.github/workflows/release.yml``)
gets a final step that publishes to PyPI when the
``publish`` input is set (default: yes). The
manual trigger ``gh workflow run release.yml -f
level=minor`` becomes the canonical "cut a
release" command.

The decision breaks into 6 sub-decisions.

### 2.1 Package name: ``kntgraph``

The package on PyPI is ``kntgraph``
(``[project]::name = "kntgraph"``). The name is
currently unregistered on PyPI (verified
2026-07-31). The first release will register it;
subsequent releases reuse the existing project.

**Alternative considered:** register under
``kinetgraph`` (the GitHub org name) and use
``kntgraph`` as a console-script alias. Rejected
because:

- The codebase is ``import kntgraph`` everywhere
  (the rename from ``fmh_backend`` happened in
  v0.7.0, ADR-036). Re-importing would be a
  breaking change for the (small) set of existing
  consumers.
- The PyPI name is the canonical "what you
  ``pip install``" identifier. Splitting it from
  the import name adds confusion.

### 2.2 First release: the next release after this ADR is Accepted

This ADR ships the **mechanism**; the first
release is the operator's call. When you are
ready:

1. Register ``kntgraph`` on PyPI as a Trusted
   Publisher (the GitHub repo
   ``kinetgraph/kinetgraph``, workflow
   ``.github/workflows/release.yml``, environment
   ``pypi``).
2. Merge this ADR.
3. The next time you cut a release, set the
   ``publish`` input to ``yes`` (the default);
   the workflow uploads to PyPI as part of the
   same run.

The first PyPI release carries the version that
the operator chose (``v0.11.0`` or ``v1.0.0`` —
the operator decides). Older releases
(``v0.7.0`` - ``v0.10.0``) are **not** uploaded;
they are git tags only. PyPI is "the latest
release plus history from there".

### 2.3 Workflow integration: the ``release.yml`` gets a final step

The existing release workflow (ADR-051 PR 4) has
8 steps. This ADR adds a 9th step after the
``gh release create`` step:

```yaml
- name: Publish to PyPI (Trusted Publishing)
  if: ${{ inputs.publish == 'yes' }}
  uses: pypa/gh-action-pypi-publish@release/v1
  with:
    packages-dir: dist/
    # The OIDC token is auto-issued by GitHub
    # when the workflow has the
    # ``id-token: write`` permission.
```

The new input is added to the
``workflow_dispatch`` block:

```yaml
inputs:
  publish:
    description: "Publish to PyPI"
    required: true
    type: choice
    options:
      - yes
      - no
    default: "yes"
```

Default is "yes" so the operator does not have
to set it on every release; the option exists
for the case "I want to cut a release tag but not
publish to PyPI yet" (e.g., the PyPI registration
is pending, or the version is tagged for
internal use only).

### 2.4 Trust boundary: a GitHub Environment named ``pypi``

Trusted Publishing requires a **GitHub
Environment** with a protection rule. The
environment is a named bucket in the repo
settings (``Settings -> Environments ->
``pypi``); the rule says "deployments to this
environment require approval from
``@kntgraph/maintainers``".

The release workflow declares:

```yaml
jobs:
  cut-release:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      contents: write
      id-token: write  # required for Trusted Publishing
```

The ``environment: pypi`` + the protection rule
mean: a malicious PR cannot trigger the
``release.yml`` workflow and exfiltrate the
OIDC token (the token is only issued for jobs
in the ``pypi`` environment, and that environment
requires maintainer approval to deploy to).

This is a **defence-in-depth** measure. The OIDC
token is short-lived and tightly scoped; the
environment rule adds a human-in-the-loop
gate. Both are recommended by [PyPI's
docs](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

### 2.5 What is built into the wheel

The ``[tool.setuptools_scm]`` config in
``pyproject.toml`` (ADR-051 PR 1) already produces
a wheel with the correct version
(``kntgraph-0.10.0-py3-none-any.whl``). The
release workflow needs to build the wheel
**before** publishing:

```yaml
- name: Build the wheel
  run: |
    uv build --wheel
    # The wheel lands in ``dist/``.
```

The ``pypa/gh-action-pypi-publish`` reads
``dist/`` by default (``packages-dir: dist/``).
No change to the source layout is required.

### 2.6 Out of scope (explicit)

- **No release notes from ``CHANGELOG.md``** on
  PyPI. PyPI's "long description" field is the
  README rendered as ``reStructuredText`` (or
  ``Markdown`` with ``--long-description-content-type=text/markdown``).
  The release workflow already opens the GitHub
  Release with the CHANGELOG section; the PyPI
  long description is a separate concern (and
  likely a simpler choice — the README's "what is
  kntgraph" section, not the version-by-version
  history).
- **No automatic ``-dev``/``-rc`` tags** on
  PyPI. Pre-releases and release candidates
  stay in the git history only; PyPI gets the
  release version (``v0.11.0``, not
  ``v0.11.0rc1``). The operator who wants a
  release candidate uploads it manually
  (``twine upload`` locally).
- **No ``yank`` automation**. The operator who
  wants to yank a release does it from the PyPI
  web UI; the workflow does not yank. (Yanking
  is a rare operation; automation adds risk for
  little gain.)
- **No mirror to a private index**. If the
  project ever needs a private PyPI mirror
  (e.g., for a customer's internal deployment),
  the operator adds a second publish step with
  a different ``repository_url``. Out of scope
  today; flagged in §5.

## 3. Consequences

### 3.1 Positive

- **One canonical install path.** ``pip install
  kntgraph`` works. ``pip show kntgraph`` shows
  the version. ``kntgraph>=0.10`` is a valid
  requirement specifier in a downstream
  ``pyproject.toml``. This is the standard
  Python ecosystem contract; today the project
  is invisible to that contract.
- **No long-lived secrets in the repo.** The
  OIDC token is per-workflow-run, signed by
  GitHub, and verified by PyPI. There is nothing
  in the GitHub secrets store that an attacker
  could leak.
- **The release workflow is end-to-end
  automated.** After this ADR, the operator's
  contribution to a release is: update the
  CHANGELOG, push the trigger, wait for the
  workflow to finish. No manual upload via
  ``twine``, no manual copy of wheels to a
  server.
- **Multi-maintainer is free.** Anyone with
  merge access to ``main`` can trigger the
  release. The OIDC identity is the GitHub
  account, not a per-person token. The
  environment approval rule keeps the human
  gate.

### 3.2 Negative

- **PyPI names are first-come-first-served.** A
  competing project (or a typo-squatter) could
  register ``kntgraph`` between the time this
  ADR is Accepted and the first release. The
  mitigation is: register the Trusted Publisher
  relationship on PyPI as **part of this ADR's
  implementation** (i.e., the operator does the
  PyPI registration on the same day the ADR
  is Accepted; the workflow config follows).
  If the name is already taken when the operator
  goes to publish, this ADR is a no-op and a
  follow-up ADR chooses a new name.
- **The Trusted Publisher config is per-repo.**
  Moving the project to a new GitHub org (or
  renaming the org) breaks the publisher. The
  mitigation is: the org rename is a project
  decision; the publish config is updated as
  part of that decision. There is no automation
  here; this is the kind of "operator's job"
  that PyPI expects.
- **A failed publish is harder to recover from
  than a failed git push.** A failed
  ``git push origin v0.11.0`` is recoverable by
  re-running the workflow; a failed PyPI upload
  (e.g., the wheel has a metadata error) leaves
  the project with a tag that says "v0.11.0
  shipped" but PyPI that says "0.11.0 is not
  there". The mitigation is: the workflow's
  dry-run guard (``python -m build`` before the
  publish step) catches metadata errors; the
  GitHub Release (the 11th step) acts as the
  "official" announcement that survives a
  failed PyPI upload.

### 3.3 Neutral

- **A new GitHub Environment is created** as
  part of this ADR. Environments are repo-level;
  the operator does the setup in the GitHub UI
  (5 minutes; one-off). The workflow's
  ``environment: pypi`` reference is the only
  code change required.
- **The release workflow gets one more step**
  (build the wheel) and one more input
  (``publish: yes|no``). The total workflow
  grows from 11 steps to 12; the operator's
  mental model grows by one decision ("publish
  to PyPI or not?").
- **PyPI is irreversible in one direction.** A
  release can be yank''d (hidden from ``pip
  install`` by default) but cannot be deleted.
  This is PyPI's policy, not the project's; the
  ADR inherits the constraint.

## 4. Migration plan

The migration is 1 PR. Each step is **independently
mergeable** and **independently revertable**.

### PR 1 — PyPI publish workflow (~1 day)

1. **Operator** (not in the repo) registers
   ``kntgraph`` on PyPI as a Trusted Publisher
   (the GitHub repo ``kinetgraph/kinetgraph``,
   workflow ``.github/workflows/release.yml``,
   environment ``pypi``). The PyPI web UI
   prompts for these values; the operator copies
   them from the workflow YAML.
2. **Operator** creates the ``pypi`` GitHub
   Environment (``Settings -> Environments ->
   New environment -> ``pypi````). Adds a
   protection rule: "Required reviewers: any
   user in the ``@kinetgraph/maintainers`` team".
3. **Code** in this PR:
   - Add the ``publish`` input to
     ``release.yml``.
   - Add the ``environment: pypi`` + the
     ``id-token: write`` permission to the
     ``cut-release`` job.
   - Add the "Build the wheel" step.
   - Add the "Publish to PyPI" step (the
     ``pypa/gh-action-pypi-publish`` action).
4. **Test**: the operator runs the workflow with
   ``publish: no`` (the default for a dry-run)
   to verify the wheel builds correctly. Then a
   second run with ``publish: yes`` to verify
   the publish step works.
5. **First release**: the operator cuts a
   release (e.g. ``v0.11.0``) via the workflow.
   PyPI receives the wheel; the project has a
   public install path.
6. **Docs**: ``README.md::Installation`` updates
   from "clone the repo" to
   ``pip install kntgraph``.

### Total time

~1 day of operator work (PyPI registration +
GitHub Environment setup + first release).
~2 hours of code (the workflow change is
mechanical; the new test is the wheel-build
guard).

## 5. Acceptance checklist

- [ ] ``kntgraph`` is registered on PyPI as a
      Trusted Publisher (the GitHub repo
      ``kinetgraph/kinetgraph``, workflow
      ``.github/workflows/release.yml``,
      environment ``pypi``).
- [ ] The ``pypi`` GitHub Environment exists with
      a "required reviewers" protection rule.
- [ ] ``release.yml`` accepts a ``publish`` input
      (``yes|no``); the publish step is gated on
      the input.
- [ ] ``release.yml`` builds the wheel before the
      publish step (the
      ``pypa/gh-action-pypi-publish`` action reads
      ``dist/``).
- [ ] The first PyPI release is cut via the
      workflow (``gh workflow run release.yml -f
      level=minor -f publish=yes``); the wheel
      lands on PyPI within a few minutes.
- [ ] ``pip install kntgraph`` works on a fresh
      environment (e.g. a CI runner that does not
      have the project checked out).
- [ ] ``pip show kntgraph`` shows the version
      derived from the git tag (ADR-051).
- [ ] ``README.md::Installation`` documents
      ``pip install kntgraph`` as the canonical
      install path.
- [ ] CI green: 11/11 gates (the existing 11
      gates; PyPI is **not** a CI gate — the
      publish step is in the release workflow, not
      in ``scripts/ci.py``).

## 6. References

  - [PEP 740 — Index Package
    Metadata](https://peps.python.org/pep-0740/) (the
    OIDC-based publish mechanism; PyPI adopted it
    as "Trusted Publishing")
  - [PyPI Trusted Publishers
    docs](https://docs.pypi.org/trusted-publishers/)
    (the operator-facing setup guide; the canonical
    source for "how to register a repo").
  - [pypa/gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish)
    (the canonical action this ADR wraps;
    maintained by the PyPA).
  - [ADR-051 — Release Versioning via Git Tags + ``setuptools_scm``](./ADR-051-Release-Versioning-via-Git-Tags.md)
    (the version is the git tag; this ADR publishes
    it to PyPI; together they are the project's
    "official" release process).
  - [GitHub Actions: Environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)
    (the protection-rule mechanism this ADR uses
    for the "human gate" on the publish step).
