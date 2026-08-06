"""Tests for check_build_freshness.py -- the advisory script that compares a
built frontend bundle's stamped source commit against the latest commit that
actually touched that project's src/. All git calls are mocked; nothing here
touches the real repo history or shells out for real.

Run: python tests/test_build_freshness.py
"""
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_build_freshness as cbf


def _run_check(tmp_root, git_log_result, merge_base_returncode=0, rev_list_result="3",
               stamp_content="deadbeef1234\n", write_stamp=True):
    """Runs check_one() against a temp REPO_ROOT with every git call mocked.

    git_log_result: stdout string (success) or an Exception instance to raise
    (simulating a `git log` failure, matching subprocess.run(check=True)).
    """
    stamp_path = "static/app/.build-commit"
    (tmp_root / "static" / "app").mkdir(parents=True, exist_ok=True)
    if write_stamp:
        (tmp_root / stamp_path).write_text(stamp_content)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "log"]:
            if isinstance(git_log_result, Exception):
                raise git_log_result
            return subprocess.CompletedProcess(cmd, 0, stdout=git_log_result, stderr="")
        if cmd[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(cmd, merge_base_returncode)
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=rev_list_result, stderr="")
        raise AssertionError(f"unexpected command in test: {cmd}")

    with patch.object(cbf, "REPO_ROOT", tmp_root), patch("subprocess.run", side_effect=fake_run):
        return cbf.check_one("app/ (test)", "app/src", stamp_path)


def test_missing_stamp_file_warns_to_build():
    with tempfile.TemporaryDirectory() as tmp:
        warning = _run_check(Path(tmp), git_log_result="abc123\n", write_stamp=False)
    assert warning is not None
    assert "no build found" in warning
    assert "npm run build" in warning


def test_empty_stamp_file_warns():
    with tempfile.TemporaryDirectory() as tmp:
        warning = _run_check(Path(tmp), git_log_result="abc123\n", stamp_content="")
    assert warning is not None
    assert "empty" in warning


def test_fresh_build_returns_no_warning():
    """The build SHA is an ancestor of (or equal to) the latest source
    commit -- merge-base --is-ancestor exits 0 -- so nothing is stale."""
    with tempfile.TemporaryDirectory() as tmp:
        warning = _run_check(Path(tmp), git_log_result="abc123\n", merge_base_returncode=0)
    assert warning is None


def test_stale_build_reports_commit_count():
    """merge-base --is-ancestor exits nonzero -- the latest source commit is
    NOT reachable from the stamped build SHA -- the build predates it."""
    with tempfile.TemporaryDirectory() as tmp:
        warning = _run_check(
            Path(tmp), git_log_result="newcommitsha\n", merge_base_returncode=1, rev_list_result="3\n",
        )
    assert warning is not None
    assert "STALE" in warning
    assert "3 commit" in warning
    assert "npm run build" in warning


def test_git_log_failure_is_reported_not_raised():
    """A git failure (corrupt repo, not a git checkout, etc.) must produce a
    warning string, never propagate as an unhandled exception -- this script
    runs as an advisory pre-deploy check and must never crash the caller."""
    with tempfile.TemporaryDirectory() as tmp:
        warning = _run_check(
            Path(tmp),
            git_log_result=subprocess.CalledProcessError(128, ["git", "log"]),
        )
    assert warning is not None
    assert "could not read git history" in warning


def test_main_never_fails_even_when_stale():
    """Advisory only -- main() must always return 0, per the script's own
    documented contract (see module docstring), even when it found and
    printed a stale bundle."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        (tmp_root / "static" / "app").mkdir(parents=True, exist_ok=True)
        (tmp_root / "static" / "landing").mkdir(parents=True, exist_ok=True)
        # Neither project has a stamp file -- both report "no build found",
        # which is still advisory, not fatal.
        with patch.object(cbf, "REPO_ROOT", tmp_root):
            assert cbf.main() == 0


# ---------------------------------------------------------------------- runner

def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"PASS  {name}")
        except Exception:
            failed.append(name)
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed, {len(failed)} failed")
    if failed:
        print("Failed:", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
