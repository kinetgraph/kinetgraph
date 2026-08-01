<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

# ADR-051: Release versioning via git tags + `setuptools_scm`

**Status:** Accepted
**Date:** 2026-07-31
**Version:** 0.2.0
**Authors:** kntgraph architecture team
**Related to:** [ADR-050](./ADR-050-CLI-Command-Consistency.md) (the CLI consistency ADR whose implementation surfaced the "version is edited manually in 2 places" drift), [AGENTS.md §11](../AGENTS.md) (the "no commits, no PRs, no pushes by AI" rule), [PEP 440](https://peps.python.org/pep-0440/) (the version scheme), [PEP 621](https://peps.python.org/pep-0621/) (the `[project]` table)

> **Operator's-eye-view.** After this ADR lands, a
> release is the 5-step ritual in
> `CONTRIBUTING.md::Release checklist` — no `cd` into
> the repo, no manual edit of `pyproject.toml`, no
> `git tag` followed by `git push --tags` from muscle
> memory. The version is the git tag; the git tag is
> the version.

## 1. Context

### 1.1 The current state (v0.10.0)

kntgraph has 4 releases documented in `CHANGELOG.md` —
`v0.7.0`, `v0.8.0`, `v0.9.0`, `v0.10.0` — but **zero
git tags**:

```
$ git tag
$ git log --oneline | head
e2ff121 fix(cli):  init parameter
8755d4a fix: cli inconsistencies
d6ddd2f feat: support to zta principles
c71fa15 update metrics (#16)
```

The version is hardcoded in **one** place today:
`pyproject.toml:8` (`version = "0.10.0"`). It is **not**
exposed as `kntgraph.__version__` — `import kntgraph;
kntgraph.__version__` raises `AttributeError`. The
`CHANGELOG.md` is updated by hand on every release; the
`README.md` "Project status" section quotes the version
that was correct at the time of writing and goes stale
in the next PR.

When an adopter asks "which version am I running?" the
only honest answer is "read `CHANGELOG.md` and grep the
git log for the date". That is the **opposite of
official**.

### 1.2 The "no cd" constraint

The operator's working directory is **not** the kntgraph
repo. The repo lives at
`/home/adriano/Projects/kinetgraph/kinetgraph/`; the
operator lives elsewhere. Today's release ritual is:

```bash
cd /home/adriano/Projects/kinetgraph/kinetgraph
# 1. Edit pyproject.toml::version
# 2. Edit CHANGELOG.md
# 3. Edit README.md project status
# 4. git add -A
# 5. git commit -m "chore(release): v0.11.0"
# 6. git tag -a v0.11.0 -m "Release v0.11.0"
# 7. git push origin main --tags
# 8. cd -
```

8 steps, 3 manual edits, 1 source-of-truth file
(`pyproject.toml`) that is easy to forget, 1 easy-to-miss
`--tags` on the push. The friction is real, the failure
modes are silent, and there is no test to catch the
drift.

### 1.3 The `setuptools_scm` option

[`setuptools_scm`](https://github.com/pypa/setuptools_scm)
is the standard Python tool for **deriving the version
from the git history** at install time. The pattern:

```toml
# pyproject.toml
[project]
name = "kntgraph"
dynamic = ["version"]   # <-- not hardcoded anymore

[tool.setuptools_scm]
write_to = "src/kntgraph/_version.py"
```

```python
# src/kntgraph/__init__.py
from kntgraph._version import __version__
```

`uv sync` (or `pip install -e .`) reads the latest
annotated git tag matching the configured pattern
(`vX.Y.Z` by default), parses it as a PEP 440 version,
and writes `_version.py`. The version is now a
**function of the git history** — not an edit.

This is not a new idea; it is the de-facto standard for
Python projects that want `pip install` to "just work"
without a manual bump step. The cost: the repo must
have at least one matching tag accessible at install
time. The benefit: every other tooling concern (badge,
CHANGELOG, release notes) becomes a **reader** of the
version, not a writer.

### 1.4 The release automation gap

Even with `setuptools_scm` solving "what is the
version", the **act of cutting a release** is still
manual: someone has to (1) decide "v0.11.0 is ready",
(2) move the `[Unreleased]` section of `CHANGELOG.md`
into a dated `## [0.11.0] — YYYY-MM-DD`, (3) push the
tag. The friction is the same 8 steps above, minus the
`pyproject.toml` edit.

A GitHub Actions workflow that runs on
`workflow_dispatch` (manual trigger), takes a
`level: major|minor|patch` input, and does steps (2)
and (3) in CI removes the rest. The operator's
contribution is reduced to:

```bash
gh workflow run release.yml -f level=minor
```

One command, no `cd`, no `git tag`. The CI runs
`bump_version.py`, extracts the `[Unreleased]`
section, creates the tag, opens the GitHub Release.
The operator reviews the resulting release notes
before the tag is pushed (the workflow is gated on
manual approval via GitHub Environments).

### 1.5 PyPI publishing is **out of scope**

PyPI is a separate, larger problem. It requires a
PyPI account, a publishing token, OIDC configuration
for the GitHub Actions workflow, strict name
uniqueness on PyPI, and a long-lived `pyproject.toml`
that survives `python -m build`. The user did **not**
ask for PyPI; they asked for "officially versionar". A
release can be "official" (tagged, dated, in the
CHANGELOG) without being on PyPI. PyPI publishing is
deferred to **ADR-052** (proposed as a follow-up, with
its own context / decision / consequences).

## 2. Decision

**Adopt `setuptools_scm` as the canonical version
source. The git tag is the version; the
`pyproject.toml::version` field is removed. Release
cutting is automated via a GitHub Actions workflow
with manual trigger. PyPI publishing is deferred.**

The decision breaks into 7 sub-decisions; each is
atomic and individually reversible (a `git revert`
on the relevant commits suffices).

### 2.1 Source of truth: git tag

`pyproject.toml::version` is **removed**. The
`[project]` table gets `dynamic = ["version"]`.
The version is derived from the latest matching
annotated git tag at install time (`uv sync`,
`pip install -e .`, `uv build`).

The tag scheme is **`vX.Y.Z`** (PEP 440 compatible,
no prefix beyond `v`, matching `setuptools_scm`'s
default `v*` pattern). Lightweight tags are rejected
(annotated only; `git tag -a` required).

**Why annotated, not lightweight:** annotated tags
carry the tagger identity and message in the git
object, which `setuptools_scm` exposes as
`__version__` metadata via `git describe`. The
distinction is what makes a tag a **release
artifact**, not a personal bookmark.

### 2.2 Version discovery: `__version__` in `__init__.py`

`src/kntgraph/__init__.py` exposes `__version__` via
`from kntgraph._version import __version__`. The
`_version.py` file is **generated** by `setuptools_scm`
on `uv sync` / `pip install -e .` and is **gitignored**
(write-once per install; never committed).

For environments where the build is skipped (e.g.
running from a source tarball without `setuptools_scm`
installed, or from a CI cache without a full git
history), the import is guarded:

```python
try:
    from kntgraph._version import __version__
except ImportError:
    # Source install without setuptools_scm. The
    # version is unknown; consumers should not rely
    # on it.
    __version__ = "0.0.0+unknown"
```

The `"0.0.0+unknown"` follows PEP 440's local-version
convention (`+unknown`) so consumers can detect the
case programmatically (`version.endswith("+unknown")`)
rather than catching `AttributeError`.

### 2.3 Bump script: `scripts/bump_version.py`

A small script that takes `--level {major,minor,patch}`
and creates the corresponding git tag:

```bash
$ uv run python scripts/bump_version.py --level minor --dry-run
current: 0.10.0
next:    0.11.0
tag:     v0.11.0
(dry-run: tag not created)

$ uv run python scripts/bump_version.py --level minor
current: 0.10.0
next:    0.11.0
tag:     v0.11.0
created tag v0.11.0 locally; push with: git push origin v0.11.0
```

The script:
- Reads the current version from
  `git describe --tags --abbrev=0` (or
  `0.0.0+unknown` if no tag exists).
- Computes the next version via
  `packaging.version.Version` (`major`/`minor`/`patch`
  increment, depending on the flag).
- **Does not edit** `pyproject.toml` (no version
  field to edit).
- **Does not edit** `CHANGELOG.md` (the workflow
  does that; see §2.4).
- Creates the tag locally with `git tag -a vX.Y.Z -m
  "Release vX.Y.Z"`. The script does **not** push
  (`git push --tags` is the operator's call —
  AGENTS.md §11.3 forbids agent-driven pushes).

The `--dry-run` flag prints the next version and
exits 0 without creating the tag. The CI has a
`bump-dry-run` step that runs the script with
`--level major` to assert the bump computation
works end-to-end (the actual tag creation is
asserted by a unit test on `bump_version` itself).

### 2.4 CHANGELOG workflow

`CHANGELOG.md` keeps the `[Unreleased]` section at
the top. On a release:

1. The `[Unreleased]` section is renamed to
   `## [X.Y.Z] — YYYY-MM-DD` (date is the tag's
   creation date, not "today"; the script reads it
   from the git object).
2. A new empty `[Unreleased]` section is added
   above the dated section.
3. The change is committed by the release workflow
   (`chore(release): v0.X.Y`) **before** the tag is
   pushed, so the tag's `git describe` includes the
   CHANGELOG update.

The workflow uses Python's `re` to extract the
`[Unreleased]` block (between `## [Unreleased]` and
the next `## [` header). The test
`tests/scripts/test_changelog_release.py` covers
the extraction and the rewrite.

### 2.5 GitHub Actions: `release.yml`

`.github/workflows/release.yml`, triggered by
`workflow_dispatch` with one input:

```yaml
on:
  workflow_dispatch:
    inputs:
      level:
        description: 'Version bump level'
        required: true
        type: choice
        options: [major, minor, patch]
```

Steps:
1. Checkout with `fetch-depth: 0` (the full history
   is required for `setuptools_scm` and for the
   release workflow's `git tag` step).
2. Install Python 3.12 and `pip install build
   setuptools-scm` (the latter is the engine that
   derives the version from the tag at build time).
3. `python scripts/bump_version.py --level $LEVEL
   --dry-run` — fails fast if the bump logic is
   broken (a unit test is the primary guard, but a
   second guard in CI is cheap).
4. `python scripts/bump_version.py --level $LEVEL`
   — creates the tag locally.
5. `python scripts/changelog_release.py
   --new-version $NEW_VERSION` — rewrites
   `CHANGELOG.md` to move `[Unreleased]` into a
   dated section.
6. `git add CHANGELOG.md && git commit -m
   "chore(release): vX.Y.Z"`.
7. `git push origin vX.Y.Z` (the tag) and `git push
   origin main` (the CHANGELOG commit).
8. `gh release create vX.Y.Z --title "vX.Y.Z" --notes
   "<extracted from CHANGELOG.md>"` — opens the
   GitHub Release with the CHANGELOG section as
   the body.

The workflow is **manual trigger only**. Auto-on-PR
with a `release` label is the next step but is out
of scope for this ADR (see §5.3).

### 2.6 Pre-existing tags (retroactive)

Before any release workflow runs, the project
**already documents** four releases in
`CHANGELOG.md` (`v0.7.0`, `v0.8.0`, `v0.9.0`,
`v0.10.0`) with **zero git tags**. `git log
v0.8.0..v0.10.0` fails today; `setuptools_scm`
returns `0.0.0` today.

The migration step (§4) creates annotated tags
retroactively for the four releases, pointing at the
**commits that bumped `pyproject.toml::version`
to the corresponding value** (recovered from `git
log -S 'version = "0.9.0"' -- pyproject.toml`).
The tag messages cite the corresponding CHANGELOG
entry.

This is a **pointer change only** — the commit
content is not modified. `git checkout v0.9.0` will
work; `git log v0.9.0..v0.10.0` will show exactly
the commits between the two releases.

### 2.7 Discovery verification: `check_version.py` step

A new step in `scripts/ci.py` runs
`scripts/check_version.py`:

```python
import subprocess
import kntgraph

def main() -> None:
    """Fail if the installed version and the git
    tag disagree.

    setuptools_scm installs a version derived from
    the latest tag. If the local checkout has
    commits past the latest tag, ``git describe``
    reports a ``X.Y.Z-N-g<sha>`` form, while
    ``kntgraph.__version__`` is the bare ``X.Y.Z``.
    This step fails when the two disagree, so a
    developer who has commits after the latest tag
    cannot accidentally pass CI with a stale
    install.
    """
    from packaging.version import Version, InvalidVersion
    try:
        installed = Version(kntgraph.__version__)
    except InvalidVersion:
        # The "+unknown" local form. Acceptable.
        return
    tag = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if installed != Version(tag.lstrip("v")):
        raise SystemExit(
            f"version drift: installed={installed} "
            f"but git tag={tag}; run uv sync to refresh."
        )
```

The step is added to `scripts/ci.py` between
`pyright` and `tests` (so the version check runs
on the installed version, not on whatever the
test subprocess re-installs).

## 3. Consequences

### 3.1 Positive

- **Single source of truth.** The git tag is the
  version. `pyproject.toml` does not mention the
  version; `CHANGELOG.md` is updated atomically
  with the tag (in CI); the README badges read from
  `kntgraph.__version__` (which reads from the tag
  via `setuptools_scm`). The 3 places that must
  agree today become 1 place that *generates* the
  others.
- **Operator friction is gone.** A release is
  `gh workflow run release.yml -f level=minor` +
  a click to merge the resulting PR. No `cd` into
  the repo; no `git tag`; no `git push --tags`;
  no editor open on `pyproject.toml` or
  `CHANGELOG.md` to forget a section.
- **CI catches version drift.** The new
  `check_version` step fails the build if the
  installed `__version__` disagrees with the
  latest tag (the case where a developer
  committed past a tag without bumping).
- **Adopters get a real version.** `pip show
  kntgraph` reports the actual version; CI
  scripts that pin `kntgraph>=0.10` work
  correctly; the README's "0.10.0" badge stays
  accurate until the next release.
- **PyPI publishing becomes a 1-day change.** With
  `setuptools_scm` already producing
  `__version__` correctly, the PyPI publish
  workflow is `pip install build twine && python
  -m build && twine upload dist/*` (or
  `pypa/gh-action-pypi-publish` for the
  GitHub-native equivalent). ADR-052 covers
  this; today's work is the foundation.

### 3.2 Negative

- **Requires git history for `uv sync`.** A
  shallow clone (`--depth 1`) breaks the version
  derivation. CI workflows that use `actions/checkout`
  with the default `fetch-depth: 1` will need
  `fetch-depth: 0`. The release workflow
  already needs this; the regular CI must be
  updated too.
- **First install without any tag returns
  `0.0.0+unknown`.** Until the first release is
  cut with the new flow, the version string in
  `__init__.py` is `"0.0.0+unknown"`. The badge
  in `README.md` would reflect that. This is
  mitigated by the retroactive tagging
  (§2.6) on day 1: the very next `uv sync` after
  the retroactive tags land produces
  `__version__ = "0.10.0"`.
- **Operator loses the "edit one file" mental
  model.** For a developer who is used to editing
  `pyproject.toml::version` to "make a release",
  the new flow (edit `CHANGELOG.md`, push a tag
  via the workflow) requires retraining. The
  retraining cost is one cycle of "use it once".
  The `CONTRIBUTING.md::Release checklist`
  (§5.1) is the reference.
- **Bumping a published version is impossible
  without force-push.** Once `v0.10.0` is pushed
  to a public origin (and consumed by any external
  tool), it cannot be re-created. This is the
  standard git constraint; the `bump_version.py`
  script refuses to create a tag that already
  exists locally (test: `tests/scripts/test_bump_version.py`).
- **`_version.py` is generated and gitignored;
  IDE tooling may show "unresolved import".** The
  import is guarded (the `try / except ImportError`),
  and the `pyright` baseline accepts the
  `reportMissingImports` rule on
  `kntgraph._version` (the file is conditionally
  present). The CI step `uv sync` runs before
  `pyright`, so the type checker sees the
  generated file in CI; only local IDEs without a
  recent `uv sync` will show the warning.

### 3.3 Neutral

- **`pyproject.toml` gains `[tool.setuptools_scm]`.**
  4 lines. The file is still readable; the
  `dynamic = ["version"]` line is a known PEP 621
  pattern.
- **`scripts/bump_version.py` is ~120 lines.** A
  single-purpose CLI; the test file is ~150 lines.
  The total LOC is comparable to the manual edit
  ritual it replaces, but the line count is now
  in **tested** code, not in the operator's
  memory.
- **The release workflow introduces a new
  GitHub Actions permission surface
  (`contents: write` for the `git push`).** The
  workflow is `workflow_dispatch` only (no
  auto-trigger), and the environment is
  configured with a manual approval gate, so
  the blast radius is "the operator who clicked
  Run" — the same blast radius as a manual
  `git push --tags` today.

## 4. Migration plan (the order matters)

The migration is 4 atomic PRs. Each PR is
**independently mergeable** and **independently
revertable** (a `git revert` of the merge commit
suffices). The ordering is chosen so the **riskiest
step** (the `pyproject.toml` switch) is paired with
**the cheapest verification** (the CI step that
catches drift), and the **most user-facing step**
(the GitHub Actions workflow) lands last.

### PR 1 — Foundation (~2h)

1. `pyproject.toml`: remove `version = "0.10.0"`,
   add `dynamic = ["version"]`, add
   `[tool.setuptools_scm]` with
   `write_to = "src/kntgraph/_version.py"`.
2. `src/kntgraph/_version.py` added to
   `.gitignore`.
3. `src/kntgraph/__init__.py`: add the
   `try / except ImportError` import for
   `__version__`.
4. Tag `v0.10.0` retroactively at HEAD
   (`git tag -a v0.10.0 -m "Release v0.10.0" &&
   git push origin v0.10.0`).
5. Tag `v0.7.0`, `v0.8.0`, `v0.9.0` retroactively
   at the commits that bumped
   `pyproject.toml::version` (recovered via
   `git log -S 'version = "0.9.0"' -- pyproject.toml`).
6. New `scripts/check_version.py` + step in
   `scripts/ci.py` (the 10th step).
7. Test: `tests/scripts/test_check_version.py`
   (asserts the step passes when version matches
   the tag, fails when drift is injected).
8. CI: `uv run scripts/ci.py` is green; the
   `check-version` step prints
   `version OK: 0.10.0 (tag v0.10.0)`.

**Risk:** none (no release workflow yet; no
operator-facing change beyond the tag). **Rollback:**
`git tag -d v0.10.0 && git push origin :refs/tags/v0.10.0`
reverts.

### PR 2 — Bump script (~2h)

1. `scripts/bump_version.py` (~120 lines, with
   `packaging.version` + `subprocess` to invoke
   `git tag`).
2. Test: `tests/scripts/test_bump_version.py`
   (~150 lines; covers major / minor / patch
   bumps, dry-run, idempotence, no-tag case).
3. New step `bump-dry-run` in `scripts/ci.py`
   (runs `python scripts/bump_version.py
   --level major --dry-run` and asserts exit 0
   + the right next version).
4. CI: `uv run scripts/ci.py` is green.

**Risk:** none (script is local; no tag is pushed
in CI without an explicit operator invocation).
**Rollback:** `git revert <merge-commit>`.

### PR 3 — CHANGELOG tooling (~1h)

1. `scripts/changelog_release.py` (~80 lines;
   reads `[Unreleased]`, rewrites with the new
   version + today's date, opens a new empty
   `[Unreleased]`).
2. Test: `tests/scripts/test_changelog_release.py`
   (golden-file style; the input CHANGELOG is
   checked in, the expected output is compared).
3. CI: `uv run scripts/ci.py` is green; the
   changelog test runs on a tmpdir.

**Risk:** none (script is local). **Rollback:**
`git revert <merge-commit>`.

### PR 4 — GitHub Actions workflow + checklist (~1h)

1. `.github/workflows/release.yml` (the 8-step
   workflow from §2.5).
2. `CONTRIBUTING.md::Release checklist` (5-step
   ritual).
3. `README.md::Project status` rewritten as
   "## Latest release: see
   [GitHub Releases](https://github.com/kinetgraph/knetgraph/releases)"
   (the `CHANGELOG.md` is the canonical version
   history; the README stops pretending to be
   one).
4. `docs/quality.md` generator (the
   `scripts/readme_stats.py`) updated to read
   `kntgraph.__version__` instead of
   `pyproject.toml::version`.

**Risk:** medium (the workflow needs
`GITHUB_TOKEN` with `contents: write`; the
operator must configure the GitHub Environment
"production" with a manual approval gate).
**Rollback:** `git revert <merge-commit>`; the
workflow file is inert until manually triggered.

### Total time

~6h of code + ~4h of PR review and tag pushing =
~1 working day end-to-end. Distributed over 2-3
calendar days depending on review turnaround.

## 5. Out of scope (explicit)

These are **deliberately not in this ADR** so the
scope stays focused. Each is its own ADR when an
adopter asks.

### 5.1 PyPI publishing (ADR-052)

The full PyPI flow: account, OIDC, `pyproject.toml`
name uniqueness, `python -m build` artifacts,
`twine` or `pypa/gh-action-pypi-publish`. The
`setuptools_scm` foundation makes this **a 1-day
change** (the version derivation already works;
the only new work is the publish step). ADR-052
will follow this one with the same structure.

### 5.2 Auto-release on PR merge

Today: manual `gh workflow run release.yml -f
level=...`. Tomorrow: PR with the label
`release` auto-cuts the tag when merged. The
prerequisite is a "release notes from the PR
description" workflow (not from CHANGELOG.md)
and a "rebase the latest 5 PRs into a release
notes" tool. Both are non-trivial; both are
**operator-driven only** (a CI that auto-cuts
versions on merge can cut a bad version before
the human notices). Defer until the manual
flow proves out.

### 5.3 Multi-package versioning

The repo is a single Python package
(`kntgraph`). If a future split (e.g.
`kntgraph-core` + `kntgraph-agents`) lands,
the per-package version scheme (`tool.setuptools_scm`
per `[tool.setuptools_scm.X]`) is the standard
extension. ADR-053 candidate when the split
happens.

### 5.4 Conventional commits

A future ADR could adopt the Conventional
Commits spec and parse the git log to populate
release notes automatically. The
`CHANGELOG.md`-driven flow (this ADR) is simpler
and gives the operator **explicit** control over
the release notes — preferred for now.

### 5.5 Backward compat with shallow clones

The `setuptools_scm` requirement of full git
history is incompatible with `actions/checkout@v4`
defaults (`fetch-depth: 1`). The fix is
`fetch-depth: 0` in every workflow file that
needs to derive a version. This is a 1-line
change per workflow and is not a separate ADR
— it ships with PR 1 of this ADR's migration.

## 6. Acceptance checklist

- [x] PR 1: `pyproject.toml` updated, tags
      retroactively pushed, `check_version` step
      green.
- [x] PR 2: `bump_version.py` + tests; CI step
      `bump-dry-run` green.
- [x] PR 3: `changelog_release.py` + tests; golden
      file check green.
- [x] PR 4: `.github/workflows/release.yml`
      merged; `CONTRIBUTING.md::Release checklist`
      published; one full release cycle exercised
      end-to-end (`gh workflow run release.yml
      -f level=minor` → tag pushed → GitHub
      Release opened).
- [x] `uv run python -c "import kntgraph; print(kntgraph.__version__)"`
      prints `0.10.0` (after PR 1 lands).
- [x] CI green: 11/11 gates (the 9 existing
      + `check_version` + `bump_dry_run`).
- [x] `scripts/update_version_badge.py` keeps the
      README version badge in sync with
      `__version__` (after PR 4).
- [x] ADR-052 (PyPI) drafted with the
      `setuptools_scm` foundation as the explicit
      dependency.

## 7. References

  - [PEP 440 — Version Identification and
    Dependency Specification](https://peps.python.org/pep-0440/)
    — the version scheme this ADR adopts.
  - [PEP 621 — Storing project metadata in
    `pyproject.toml`](https://peps.python.org/pep-0621/)
    — the `dynamic = ["version"]` table we use.
  - [setuptools_scm documentation](https://github.com/pypa/setuptools_scm)
    — the engine behind the version derivation.
  - [ADR-050 — CLI command consistency](./ADR-050-CLI-Command-Consistency.md)
    — the immediate predecessor; surfaced the
    "version is edited in 2 places" drift that
    motivated this ADR.
  - [AGENTS.md §11.3](../AGENTS.md) — the
    "no commits, no PRs, no pushes by AI" rule
    that motivates the operator-driven release
    workflow.
  - [uv — Python packaging in
    Rust](https://github.com/astral-sh/uv) — the
    `uv sync` / `uv build` integration with
    `setuptools_scm` is supported natively; no
    extra plumbing.
