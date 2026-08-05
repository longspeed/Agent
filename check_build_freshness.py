#!/usr/bin/env python3
"""Advisory check: does each built frontend bundle reflect its current source?

Compares the commit SHA each build was stamped with (written by the
build-commit-stamp Vite plugin in app/vite.config.ts and site/vite.config.ts,
into static/app/.build-commit and static/landing/.build-commit) against the
latest commit that actually touched that project's src/. Commit SHAs, not
dates: a rebase/amend/cherry-pick changes a commit's date without changing
its content, so date comparison can read a rebased tree as "newer" than an
identical build, or the reverse (see review 2026-08-05, which found both
app/ and site/ frozen days behind already-landed source fixes this way).

Advisory only -- always exits 0. There is no CI to consume a hard failure
yet (see claude.md Health Stack), and a check that can itself fail the run
is a worse regression than the staleness it exists to catch. Run it, read
the output, rebuild if it warns -- that instruction, not this script, is
what actually closes the gap.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# (label, source dir relative to repo root, stamp file the Vite plugin writes)
PROJECTS = [
    ("app/ (settings, getting-started)", "app/src", "static/app/.build-commit"),
    ("site/ (landing page)", "site/src", "static/landing/.build-commit"),
]


def _git(*args) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def check_one(label: str, src_dir: str, stamp_path: str) -> str | None:
    """Returns a warning string, or None if fresh (or nothing to compare)."""
    stamp_file = REPO_ROOT / stamp_path
    if not stamp_file.exists():
        project_dir = src_dir.rsplit("/", 1)[0]
        return f"{label}: no build found ({stamp_path} missing) -- run `cd {project_dir} && npm run build`"
    build_sha = stamp_file.read_text().strip()
    if not build_sha:
        return f"{label}: {stamp_path} is empty"
    try:
        latest_src_commit = _git("log", "-1", "--format=%H", "--", src_dir)
    except subprocess.CalledProcessError as e:
        return f"{label}: could not read git history for {src_dir}/ ({e})"
    if not latest_src_commit:
        return None  # src/ has no commits yet -- nothing to compare against
    is_ancestor = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", latest_src_commit, build_sha],
            cwd=REPO_ROOT,
            capture_output=True,
        ).returncode
        == 0
    )
    if is_ancestor:
        return None
    try:
        commits_behind = _git("rev-list", "--count", f"{build_sha}..{latest_src_commit}")
    except subprocess.CalledProcessError:
        commits_behind = "unknown number of"
    project_dir = src_dir.rsplit("/", 1)[0]
    return (
        f"{label}: STALE -- built from {build_sha[:7]}, but {src_dir}/ has "
        f"{commits_behind} commit(s) since then. Run: cd {project_dir} && npm run build"
    )


def main() -> int:
    warnings = []
    for label, src_dir, stamp_path in PROJECTS:
        warning = check_one(label, src_dir, stamp_path)
        if warning:
            warnings.append(warning)

    if not warnings:
        print("Build freshness: all bundles match their current source.")
        return 0

    print("Build freshness: STALE BUNDLES FOUND\n")
    for warning in warnings:
        print(f"  - {warning}")
    print("\nAdvisory only (always exits 0) -- see claude.md Health Stack.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
