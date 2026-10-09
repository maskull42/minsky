"""T16 addendum (host-owned): merge commits run the pre-commit guards (F2).

Written by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 2 Oct 2026, on the researcher's ruling "F1 + F2 now
(Recommended)" (relayed by the orchestrator). The judgements encoded:
- before F2, a clean `git merge` created its commit without running pre-commit, so a branch carrying a rewritten lifecycle
  TSV could be merged unguarded;
- with pre-merge-commit, the same merge is refused before any commit exists, HEAD is unchanged and the merge can be aborted;
- an append-only change still merges, so the hook adds no new restriction of its own.
Fast-forward merges and --no-verify are outside every hook; they are documented, not tested.
"""
from __future__ import annotations

import os
from pathlib import Path

import test_lifecycle_w8_register as w8
from test_lifecycle_w8_register import HOOK, _git

repo = w8.repo                                   # re-exported pytest fixture

MERGE_HOOK = HOOK.parent / "pre-merge-commit"
REGISTER = w8.lr.REGISTER_PATH


def _side_branch_change(root: Path, data: bytes) -> str:
    """Baseline register on main; `side` rewrites it; main gains an unrelated commit (so no fast-forward)."""
    path = root / REGISTER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"header\nfirst\nsecond\n")
    _git(root, "add", "--", str(REGISTER))
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "baseline register")
    _git(root, "checkout", "--quiet", "-b", "side")
    path.write_bytes(data)
    _git(root, "add", "--", str(REGISTER))
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "side change (hooks bypassed)")
    _git(root, "checkout", "--quiet", "main")
    (root / "unrelated.txt").write_text("main moves on\n", encoding="utf-8")
    _git(root, "add", "--", "unrelated.txt")
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "unrelated main commit")
    _git(root, "config", "core.hooksPath", str(HOOK.parent))
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def test_merge_hook_is_an_executable_exec_of_pre_commit() -> None:
    assert MERGE_HOOK.is_file() and os.access(MERGE_HOOK, os.X_OK)
    lines = [line for line in MERGE_HOOK.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
    assert [line for line in lines if line.strip()] == ['exec "$(dirname "$0")/pre-commit" "$@"']


def test_clean_merge_of_a_rewritten_register_is_refused(repo: Path) -> None:
    """Mutant killed: 'no pre-merge-commit hook' (the pre-F2 state: this merge succeeded unguarded)."""
    head = _side_branch_change(repo, b"header\nedited\nsecond\n")
    result = _git(repo, "merge", "--no-ff", "--no-edit", "side", check=False)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "GUARD 4" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head            # no merge commit exists
    _git(repo, "merge", "--abort")
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_clean_merge_of_an_append_only_register_succeeds(repo: Path) -> None:
    head = _side_branch_change(repo, b"header\nfirst\nsecond\nthird\n")
    result = _git(repo, "merge", "--no-ff", "--no-edit", "side", check=False)
    assert result.returncode == 0, result.stderr
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert parents[1] == head and len(parents) == 3                          # a real two-parent merge commit
