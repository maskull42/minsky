#!/usr/bin/env python3
"""
Scope detector for /minsky.

Resolves an invocation into a canonical scope record:
  {
    "audit_id":        <str>,             # explicit or auto-generated
    "scope_kind":      "explicit"|"delta"|"paths"|"time",
    "files":           [<path>, ...],     # repo-relative paths unless --allow-external is used
    "scope_description": <str>,           # human-readable summary
    "branch":          <str>,
    "commit_at_start": <full sha>
  }

Four modes (selected by argv shape):

  explicit:  /minsky <mode> <task-id>
             First positional is a slug-shaped name (no path separators, no '--' prefix);
             skill prompts user interactively for files.

  delta:     /minsky <mode>            # no scope args, no positional task-id
             Scope = git diff from last-audit-finish-commit on this branch (queried
             from audits.db via audit-db.py last-audit-commit) to HEAD + uncommitted.

  paths:     /minsky <mode> path/to/file.md src/pipeline/
             First positional is a real path on disk; treats all positionals as paths.

  time:      /minsky <mode> --since "2 hours ago"
             Uses git log --since=... to find changed files in that window.

Output: JSON on stdout. Loud failure on any unresolvable scope.

Usage:
  scope-detect.py <mode> [args...]    # mode is one of audit|plan|draft|eval|bug-hunt
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

VALID_MODES = {"audit", "plan", "draft", "eval", "bug-hunt"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")  # explicit task-id pattern


def find_repo_root(start: Path | None = None) -> Path:
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / ".git").exists() or (candidate / ".minsky").exists():
            return candidate
    sys.exit(f"scope-detect: cannot locate repo root from {p}")


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def repo_relative_or_allowed(path: Path, repo: Path, allow_external: bool) -> str:
    resolved = path.resolve()
    if is_relative_to(resolved, repo):
        return str(resolved.relative_to(repo))
    if allow_external:
        return str(resolved)
    sys.exit(
        f"scope-detect: refusing absolute path outside repo without --allow-external: {resolved}"
    )


def run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.rstrip("\n")
    except subprocess.CalledProcessError as e:
        sys.exit(f"scope-detect: git {' '.join(args)} failed: {e.stderr.strip()}")


def current_branch(repo: Path) -> str:
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo)


def head_commit(repo: Path) -> str:
    return run_git(["rev-parse", "HEAD"], repo)


def last_audit_commit_for_branch(repo: Path, branch: str) -> str | None:
    """Query audit-db.py for the most recent finished audit's commit on this branch."""
    helper = repo / ".claude" / "skills" / "minsky" / "scripts" / "audit-db.py"
    if not helper.is_file():
        sys.exit(f"scope-detect: cannot find audit-db.py at {helper}")
    result = subprocess.run(
        ["python3", str(helper), "last-audit-commit", "--branch", branch],
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout.strip()
    return out if out else None


def auto_id(prefix: str, branch: str) -> str:
    today = datetime.date.today().isoformat()
    safe_branch = re.sub(r"[^a-z0-9_-]+", "-", branch.lower()).strip("-") or "main"
    # caller ensures uniqueness by appending a sequence number if needed
    return f"auto-{prefix}-{safe_branch}-{today}"


def changed_paths(repo: Path, base_ref: str | None) -> list[str]:
    """Return paths changed from base_ref..HEAD plus uncommitted changes (staged + unstaged)."""
    paths: set[str] = set()
    if base_ref:
        committed = run_git(["diff", "--name-only", f"{base_ref}..HEAD"], repo)
        paths.update(p for p in committed.splitlines() if p)
    # uncommitted (working tree against HEAD) — include both staged and unstaged
    uncommitted = run_git(["diff", "--name-only", "HEAD"], repo)
    paths.update(p for p in uncommitted.splitlines() if p)
    untracked = run_git(["ls-files", "--others", "--exclude-standard"], repo)
    paths.update(p for p in untracked.splitlines() if p)
    return sorted(paths)


def resolve_explicit(repo: Path, task_id: str, branch: str) -> dict:
    return {
        "audit_id": task_id,
        "scope_kind": "explicit",
        "files": [],   # supplied interactively by skill body
        "scope_description": f"Explicit task '{task_id}' (artifacts to be supplied interactively)",
        "branch": branch,
        "commit_at_start": head_commit(repo),
    }


def resolve_delta(repo: Path, branch: str) -> dict:
    base = last_audit_commit_for_branch(repo, branch)
    if base is None:
        # First audit ever on this branch — fall back to "since branch root"
        # (loud-failure-by-default isn't right here: this is the documented expected
        # state on first run. We surface it via scope_description rather than failing.)
        files = changed_paths(repo, None)
        desc = ("First audit on this branch (no prior audit in DB). "
                "Scope = uncommitted + untracked changes only.")
    else:
        files = changed_paths(repo, base)
        desc = f"Delta scope: git diff {base[:8]}..HEAD + uncommitted/untracked"
    return {
        "audit_id": auto_id("delta", branch),
        "scope_kind": "delta",
        "files": files,
        "scope_description": desc,
        "branch": branch,
        "commit_at_start": head_commit(repo),
    }


def resolve_paths(repo: Path, paths_arg: list[str], branch: str, allow_external: bool) -> dict:
    """Validate that paths exist; return repo-relative paths unless explicitly external."""
    abs_paths = []
    for p in paths_arg:
        ap = (repo / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
        if not ap.exists():
            sys.exit(f"scope-detect: path does not exist: {p}")
        abs_paths.append(repo_relative_or_allowed(ap, repo, allow_external))
    safe_first = re.sub(r"[^a-z0-9_-]+", "-", abs_paths[0].lower()).strip("-")[:40]
    return {
        "audit_id": auto_id(f"paths-{safe_first}", branch),
        "scope_kind": "paths",
        "files": abs_paths,
        "scope_description": f"Explicit paths: {', '.join(abs_paths)}",
        "branch": branch,
        "commit_at_start": head_commit(repo),
    }


def resolve_time(repo: Path, since_spec: str, branch: str) -> dict:
    """Use git log --since=SPEC to find committed changes; include uncommitted always."""
    try:
        committed = run_git(
            ["log", f"--since={since_spec}", "--name-only", "--pretty=format:"],
            repo,
        )
    except SystemExit:
        sys.exit(f"scope-detect: invalid --since spec: {since_spec!r}")
    paths = {p for p in committed.splitlines() if p}
    paths.update(p for p in run_git(["diff", "--name-only", "HEAD"], repo).splitlines() if p)
    paths.update(p for p in run_git(["ls-files", "--others", "--exclude-standard"], repo).splitlines() if p)
    safe_spec = re.sub(r"[^a-z0-9_-]+", "-", since_spec.lower()).strip("-")[:40]
    return {
        "audit_id": auto_id(f"since-{safe_spec}", branch),
        "scope_kind": "time",
        "files": sorted(paths),
        "scope_description": f"Time-based scope: git log --since={since_spec!r} + uncommitted/untracked",
        "branch": branch,
        "commit_at_start": head_commit(repo),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve /minsky invocation into canonical scope")
    parser.add_argument("mode", choices=sorted(VALID_MODES))
    parser.add_argument("rest", nargs=argparse.REMAINDER,
        help="Remaining args: explicit task-id, paths, or '--since <spec>'")
    args = parser.parse_args(argv)

    repo = find_repo_root()
    branch = current_branch(repo)

    rest = list(args.rest)
    allow_external = False
    if "--allow-external" in rest:
        rest.remove("--allow-external")
        allow_external = True

    # --since <spec> — anywhere in rest
    if "--since" in rest:
        idx = rest.index("--since")
        if idx + 1 >= len(rest):
            sys.exit("scope-detect: --since requires a value (e.g. --since '2 hours ago')")
        spec = rest[idx + 1]
        scope = resolve_time(repo, spec, branch)
    elif not rest:
        scope = resolve_delta(repo, branch)
    else:
        first = rest[0]
        # explicit task-id: matches slug pattern AND no such path on disk
        first_path = repo / first
        if SLUG_RE.match(first) and not first_path.exists() and len(rest) == 1:
            scope = resolve_explicit(repo, first, branch)
        else:
            # paths mode
            scope = resolve_paths(repo, rest, branch, allow_external)

    print(json.dumps(scope, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
