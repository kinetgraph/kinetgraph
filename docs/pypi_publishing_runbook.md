<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# Runbook: PyPI publishing (ADR-052)

This is the operator-facing runbook for
[ADR-052](./ADRs/ADR-052-PyPI-Publishing.md). It
covers the **one-time setup** (PyPI registration,
GitHub Environment) plus the **per-release flow**
(`gh workflow run release.yml` → `gh workflow run
publish.yml`).

Read this once end-to-end before the first
publish; the second publish only needs §3 onward.

## Prerequisites

- Maintainer access on
  `github.com/kinetgraph/kinetgraph` (to create
  GitHub Environments, merge PRs, approve
  deployments).
- Maintainer access on
  [pypi.org/project/kntgraph](https://pypi.org/project/kntgraph/)
  (to register the trusted publisher).
- The `gh` CLI authenticated against the org:
  ```bash
  gh auth status
  ```

If you are not yet a maintainer on both, your job
in this runbook is to **delegate** to someone who
is. PyPI does not have an "approval" flow for
trusted-publisher registration; the person who
clicks the button owns the binding until the next
project migration.

---

## 1. One-time setup (~10 minutes)

Two side-effects, **both must land before the
first publish**:

| Step | Where | What you do | Reversible? |
|---|---|---|---|
| 1.1 | [pypi.org](https://pypi.org/) | Register `kntgraph` as a Trusted Publisher (see §1.1 below) | Yes — PyPI lets you delete the binding |
| 1.2 | GitHub repo settings | Create the `pypi` Environment with a "required reviewers" rule | Yes — delete the environment |

Order matters: GitHub does not validate the
Environment name in the Trusted Publisher form,
but PyPI does not have an `environment` enum
beyond "this workflow runs with this token
shape". The Environment is the project's
defence-in-depth; the Trusted Publisher binding
is what PyPI enforces.

### 1.1 PyPI: register the trusted publisher

1. Go to
   [pypi.org/manage/account/publishing/](https://pypi.org/manage/account/publishing/).
2. Click **"Add a new pending publisher"**
   (or the project's pending-publisher form if
   you are already an owner of `kntgraph`; in
   that case the form is on the project page).
3. Fill the form **exactly** as below — every
   field is matched by PyPI's OIDC claim check,
   so a typo breaks the publish with no useful
   error message:

   | Field | Value |
   |---|---|
   | **PyPI project name** | `kntgraph` |
   | **Owner** | `kinetgraph` |
   | **Repository** | `kinetgraph/kinetgraph` |
   | **Workflow filename** | `publish.yml` (the file path relative to `.github/workflows/`, **not** the workflow name `publish`) |
   | **Environment name** | `pypi` (must match the GitHub Environment name in §1.2) |
   | **Decryption for attestations** | off (default) |

4. Click **Add**. PyPI shows a confirmation;
   you can verify at
   <https://pypi.org/manage/account/publishing/>.
5. **Pitfall:** the "Workflow filename" field
   asks for the YAML file path
   (`.github/workflows/publish.yml`), not the
   workflow display name (`publish`). If you
   paste just `publish`, the publish step
   succeeds for ~30 seconds and then PyPI
   rejects the OIDC token with
   `invalid-publisher`. Re-open the form and
   correct the path.

### 1.2 GitHub: create the `pypi` Environment

1. Open
   `https://github.com/kinetgraph/kinetgraph/settings/environments`.
2. Click **"New environment"**, type `pypi`,
   click **"Create environment"**.
3. Under **"Deployment protection rules"**,
   tick **"Required reviewers"** and add the
   `@kinetgraph/maintainers` team (or your
   team's equivalent; this is the human gate on
   the publish step).
4. **Do not** configure deployment branches;
   the workflow uses the tag as the deployment
   source.

**Verify:** the `publish.yml` workflow file
references `environment: pypi` (it does — see
`.github/workflows/publish.yml:64`). PyPI's
Trusted Publisher form accepts the Environment
name without checking it; the **environment rule
is what blocks a malicious PR**.

---

## 2. One-time README flip (one PR, after §1)

Once §1 lands but before the first PyPI upload,
flip `README.md::Install` from
`git+https://...` to `pip install kntgraph` as
canonical. The git+ form stays as a fallback
footnote for users who want the unreleased
`main`.

**Co-ordinate the timing:** §1 (the PyPI
registration) must succeed before this lands,
otherwise the README claims `pip install
kntgraph` works on an empty index. The reverse
order — flip the README first, register
afterward — is worse: PyPI indexing is not
instant, so the new install path fails for
several minutes after the README change.

```bash
# The commit that flips the README:
git checkout -b docs/pypi-install-canonical
# edit README.md::Install
git add README.md
git commit -m "docs: mark pip install kntgraph as canonical"
gh pr create --title "docs: pip install kntgraph as canonical"
```

After §1 lands + the README is flipped, the
project exposes the new install path but no
release exists yet (`pip install kntgraph` will
fail with `No matching distribution found` until
§3 lands). That window is short (minutes) and
worth living with for the readability gain.

---

## 3. Per-release flow (every release, ~5 minutes)

Once §1 + §2 are done, the per-release workflow
is two `gh` commands. Each is the canonical
"how you cut a release" / "how you publish a
release" command.

### 3.1 Cut the tag

```bash
gh workflow run release.yml \
    --repo kinetgraph/kinetgraph \
    -f level=minor \
    -f date=2026-08-01
```

The `level` is `{major,minor,patch}` (per
[ADR-051](./ADRs/ADR-051-Release-Versioning-via-Git-Tags.md));
the `date` is the CHANGELOG stamp and defaults
to today. The workflow:

1. Runs `bump_version.py` (dry-run first, then
   real).
2. Runs `changelog_release.py` to move the
   `[Unreleased]` block to a dated section.
3. Commits the CHANGELOG and creates the tag.
4. Pushes the tag and opens the GitHub Release.

**Wait for the green check** before §3.2 — the
tag exists on the remote only after the
"Push the tag" step succeeds. The GitHub Release
opening (`gh release create`) is the signal that
the workflow ended; if `gh release list` shows
the new tag, the publish in §3.2 is safe.

### 3.2 Publish to PyPI

```bash
gh workflow run publish.yml \
    --repo kinetgraph/kinetgraph \
    -f tag=v0.11.0
```

The `tag` is the git tag the operator wants to
publish. The workflow:

1. Checks out at the tag (full history so
   `setuptools_scm` derives the version).
2. Runs the sanity check (`__version__` starts
   with the tag's stripped `v`).
3. Builds the wheel (`uv build --wheel`).
4. Verifies the wheel file was produced.
5. Calls `pypa/gh-action-pypi-publish@release/v1`
   (the canonical PyPA action).
6. Runs `twine check` as defence-in-depth.

The job is in the `pypi` Environment, so the
**publish step requires maintainer approval**.
One of the maintainers on the
`@kinetgraph/maintainers` team must click
"Approve" on the deployment review screen
within 30 days; otherwise GitHub cancels the
deployment.

**Pitfall:** the `gh workflow run publish.yml`
command itself triggers the workflow, but the
publish **step** is gated by the Environment
approval. If you are the approver, the run
waits on your click, not on the trigger. Other
maintainers need to approve.

### 3.3 Verify in a fresh environment

The publish workflow's last step prints the
install hint. Run it in a directory that does
not have the repo checked out (the test is that
PyPI serves the wheel, not your local clone):

```bash
mkdir /tmp/kntgraph-verify && cd /tmp/kntgraph-verify
python -m venv .venv && source .venv/bin/activate
pip install kntgraph
python -c "import kntgraph; print(kntgraph.__version__)"
```

The version output should be `0.11.0` (or
whichever tag you published), with **no**
`+g<sha>` suffix (the `+g<sha>` indicates
`setuptools_scm` ran in `dev` mode — see
ADR-051 §3.3 for the canonical fix).

---

## 4. Failure modes (and what to do)

### 4.1 The sanity check fails (`Version mismatch: expected ...`)

The operator typed the wrong tag, or the
checked-out tag is older than the version
`setuptools_scm` derived. Do **not** try to
re-run with the same input; investigate first:

- Check the tag exists:
  `git ls-remote --tags origin v0.11.0`.
- Check the workflow's checkout step succeeded
  at the right commit (the run log shows
  `HEAD = ...`).
- If the tag exists but `__version__` is wrong,
  the source tree is stale — `uv lock --upgrade`
  in the workflow is not the fix; the issue is
  that the tag points to a commit whose
  `[tool.setuptools_scm] section` in
  `pyproject.toml` differs from the current
  `main`. This ADR does not cover that scenario
  (it would be a follow-up ADR).

### 4.2 The PyPI action fails with `invalid-publisher`

The Trusted Publisher binding in
[pypi.org/manage/account/publishing/](https://pypi.org/manage/account/publishing/)
**does not match** what the workflow sent. The
usual culprits:

- "Workflow filename" field is `publish`
  instead of `.github/workflows/publish.yml`.
- "Environment name" field is empty or
  mismatches the GitHub Environment name.
- The repository was renamed / transferred.

Re-open the form and correct; the next run
succeeds.

### 4.3 The PyPI action fails with `File already exists`

You tried to re-upload the **same version** of
the wheel. PyPI refuses duplicates by default
(`disable-upload` is the default for first-time
project owners; see
[PyPI help](https://pypi.org/help/) → "How do I
avoid having my files overwritten?").

**Do not** try to bump the version to the same
number with a post-release tag like `+local` —
the Trusted Publisher upload goes to the PyPI
server, which only knows the tag-derived
version. The fix is to cut a new tag (e.g.
`v0.11.1`) and publish that.

If the duplicate is intentional (a rebuild of
the same source, e.g. with corrected metadata),
yank the previous release from the PyPI web UI
and re-publish the same tag.

### 4.4 The wheel build fails (`uv build --wheel` errors)

The source tree does not build a wheel. The
workflow's `verify the wheel was built` step
catches this with a clear diagnostic (`no wheel
found in dist/`). Look at the `uv build` log
first:

- `ModuleNotFoundError` during the build: a
  runtime import path is missing in the wheel's
  `__init__.py`. This is a code bug; cut a
  hotfix tag (`v0.11.1`) once the bug is
  fixed.
- Metadata error
  (` Multiple `__init__.py` ...`, etc.): a
  `pyproject.toml` mistake. Same fix — hotfix
  tag.

### 4.5 The GitHub Release was created but PyPI shows nothing

The two workflows are decoupled (this is the
point of the split). The GitHub Release
publishes the **announcement**; the PyPI
upload is a **separate step**. If the publish
in §3.2 failed, the world sees the GitHub
Release but `pip install kntgraph` returns
`No matching distribution found`.

The "A failed publish leaves the project with a
tag that says shipped but PyPI that says not
there" risk in ADR-052 §3.2 is mitigated by
this runbook: §3.3 verifies in a fresh
environment, which makes the mismatch visible
within minutes. The follow-up is the same as
§4.4 (hotfix tag once the root cause is fixed).

### 4.6 A malicious PR triggers `publish.yml`

The `publish.yml` workflow is
`workflow_dispatch`-only (manual trigger); a PR
cannot trigger it. But even if a malicious PR
modified `publish.yml`, the **Trusted Publisher
binding** would still match the legitimate
`publish.yml` (PyPI binds to the workflow file
**on `main`**, not on the PR branch). The
defence-in-depth is the `pypi` Environment's
required-reviewers rule (a malicious PR cannot
approve its own deployment).

---

## 5. Recovery: yanking a release

PyPI cannot delete a release, but it can
**yank** it (`pip install kntgraph` skips yanked
versions by default; `pip install kntgraph==
0.11.0` still works until the version is
explicitly unpinned).

1. Open
   <https://pypi.org/project/kntgraph/#history>.
2. Pick the version, click **"Yank"**, choose
   the reason.
3. For the same-day fix: cut a new tag
   (`v0.11.1`) and publish it (§3). Pin the
   `pyproject.toml` requirement in
   `CONTRIBUTING.md` to
   `kntgraph>=0.11.1,<0.12` if the yank reason
   is a security fix.

Yanking does **not** affect the git tag (which
stays); it only affects PyPI's index. Adopters
who pinned their dependency to the yanked
version continue to get it from PyPI's
"specific version" path until they upgrade.

---

## 6. Acceptance checklist (mirror of ADR-052 §5)

Use this as a one-time punch list at the end of
§1 + §2 + §3:

- [ ] `kntgraph` is registered on PyPI as a
      Trusted Publisher (workflow file
      `.github/workflows/publish.yml`,
      environment `pypi`).
- [ ] The `pypi` GitHub Environment exists with
      a "required reviewers" protection rule.
- [ ] `publish.yml` accepts the `tag` input;
      the workflow builds the wheel from the tag
      and uploads via
      `pypa/gh-action-pypi-publish`.
- [ ] `release.yml` does not contain the PyPI
      publish step (the 13 contract tests in
      `tests/scripts/test_workflow_split.py`
      enforce this; they pass in CI).
- [ ] The first PyPI release is cut via
      `gh workflow run release.yml` + `gh
      workflow run publish.yml`; the wheel
      lands on PyPI within a few minutes.
- [ ] `pip install kntgraph` works on a fresh
      environment.
- [ ] `pip show kntgraph` shows the version
      derived from the git tag (ADR-051).
- [ ] `README.md::Installation` documents
      `pip install kntgraph` as the canonical
      install path.
- [ ] CI green: 11/11 gates.

---

## 7. Related documents

- [ADR-052](./ADRs/ADR-052-PyPI-Publishing.md)
  — the decision (why Trusted Publishing, why
  the workflow split).
- [ADR-051](./ADRs/ADR-051-Release-Versioning-via-Git-Tags.md)
  — the release-side counterpart (git tag is
  the version; `setuptools_scm` derives
  `__version__`).
- [CONTRIBUTING.md::Release
  checklist](./CONTRIBUTING.md#release-checklist-adr-051)
  — the per-tag release ceremony; this runbook
  is the publish-only extension.
- [PyPI Trusted Publishers
  docs](https://docs.pypi.org/trusted-publishers/)
  — the canonical reference for the registration
  form.
- [GitHub Actions:
  Environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)
  — the Environment protection-rule mechanism.
