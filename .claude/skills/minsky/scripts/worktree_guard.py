"""Refuse linked worktrees with materialised audit or Minsky state."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class WorktreeRefused(RuntimeError):
    """A Git query failed or a linked worktree has unsafe materialisation."""


def check_not_linked_worktree(start: Path) -> dict:
    """Refuse linked worktrees unless both protected trees are absent or overridden."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(start), *args], env=env, text=True, capture_output=True,
            )
        except OSError as exc:
            raise WorktreeRefused(f"refused worktree check at {start}: {exc}") from exc

    dirs = git("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    if dirs.returncode:
        if "not a git repository" in dirs.stderr:
            return {"status": "not-a-git-work-tree"}
        raise WorktreeRefused(f"refused Git directories at {start}: {dirs.stderr.strip()}")
    lines = dirs.stdout.splitlines()
    if len(lines) != 2 or any(not Path(line).is_absolute() for line in lines):
        raise WorktreeRefused(f"refused invalid Git directories at {start}: {dirs.stdout!r}")
    details = {"git_dir": str(Path(lines[0]).resolve()),
               "git_common_dir": str(Path(lines[1]).resolve())}
    top = git("rev-parse", "--path-format=absolute", "--show-toplevel")
    if top.returncode and "must be run in a work tree" in top.stderr:
        return {"status": "not-a-git-work-tree", **details}
    if top.returncode or not Path(top.stdout.strip()).is_absolute():
        raise WorktreeRefused(
            f"refused Git toplevel at {start}, git dir {details['git_dir']}, "
            f"common dir {details['git_common_dir']}: {top.stderr.strip() or top.stdout!r}"
        )
    toplevel = Path(top.stdout.rstrip("\n")).resolve()
    details["toplevel"] = str(toplevel)
    if details["git_dir"] == details["git_common_dir"]:
        return {"status": "main-checkout", **details}

    try:
        entries = subprocess.run(
            ["git", "-C", str(toplevel), "ls-files", "-t", "--", "codex-audits", ".minsky"],
            env=env, text=True, capture_output=True,
        )
    except OSError as exc:
        raise WorktreeRefused(
            f"refused skip-worktree query: git dir {details['git_dir']}, "
            f"common dir {details['git_common_dir']}: {exc}"
        ) from exc
    failed = []
    if entries.returncode:
        failed.append(f"skip-worktree query failed: {entries.stderr.strip()}")
    else:
        for entry in entries.stdout.splitlines():
            if not entry.startswith("S "):
                failed.append(f"skip-worktree tag is not S: {entry}")
                break
    for name in ("codex-audits", ".minsky"):
        path = toplevel / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            failed.append(f"on-disk absence check failed for {path}: {exc}")
        else:
            failed.append(f"on-disk absence condition failed: {path} is present")
    if not failed:
        return {"status": "linked-worktree-sparse-ok", **details}
    if os.environ.get("MINSKY_ALLOW_WORKTREE") == "1":
        return {"status": "linked-worktree-override", **details}
    raise WorktreeRefused(
        f"refused linked worktree: git dir {details['git_dir']}, "
        f"common dir {details['git_common_dir']}; {'; '.join(failed)}"
    )
