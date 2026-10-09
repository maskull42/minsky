#!/usr/bin/env python3
"""Census every round read-only; refuse internal reports, omissions and legacy drift."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

SKILL_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import provenance
import round_verify
from byte_sources import LiveSource
from lifecycle_register import RegisterError


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RegisterError(f"refused module {path}: no Python loader")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lifecycle = _module("w9_lifecycle", SKILL_DIR / "scripts/minsky-lifecycle.py")
legacy = _module("w9_legacy", SKILL_DIR / "tests/fixtures/legacy_reconcile_frozen.py")


def _component(value: str, label: str) -> str:
    if (not value or value in (".", "..") or any(c in value for c in "/\\\t\r\n\0")):
        raise RegisterError(f"refused {label}: unsafe report component {value!r}")
    return value


def discover(repo: Path, *, include_extended: bool) -> dict[str, dict]:
    """Use W8 marker discovery; refuse conflicting identities or unauthorized external links."""
    found = lifecycle.discover_rounds(repo, {})
    if include_extended:
        targets = set()
        for directory, _, files in lifecycle._walk(repo, set()):
            for name in files:
                link = directory / name
                if not stat.S_ISLNK(link.lstat().st_mode):
                    continue
                target = Path(os.path.abspath(link.parent / os.readlink(link)))
                if not target.is_relative_to("/Volumes"):
                    continue
                target = target.resolve(strict=True)
                if not target.is_relative_to("/Volumes"):
                    raise RegisterError(f"refused extended audit link {link}: resolves outside /Volumes")
                if not target.is_dir() or target in targets:
                    continue
                targets.add(target)
                for audit, entry in lifecycle.discover_rounds_in(target).items():
                    if audit in found and found[audit]["audit_dir"] != entry["audit_dir"]:
                        raise RegisterError(f"refused regression audit_id {audit}: multiple audit directories")
                    if audit in found:
                        found[audit]["rounds"] = sorted(set(found[audit]["rounds"] + entry["rounds"]))
                    else:
                        found[audit] = entry
    return found


def lstat_census(root: Path) -> dict[str, tuple]:
    """Refuse unreadable round entries; record links without following their targets."""
    result = {}

    def visit(path: Path) -> None:
        info = path.lstat()
        result[path.relative_to(root).as_posix()] = (
            info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_dev, info.st_ino, info.st_nlink, info.st_uid, info.st_gid,
            os.readlink(path) if stat.S_ISLNK(info.st_mode) else None,
        )
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)

    visit(root)
    return result


def _capture(fn: Callable[..., Any], **kwargs: Any) -> dict:
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            value = fn(**kwargs)
        return {"kind": "return", "value": value}
    except SystemExit as exc:
        return {"kind": "exit", "code": exc.code, "stderr": err.getvalue()}


def compare_legacy(round_dir: Path) -> list[dict]:
    """Compare every W2 canonical target; refuse skipping failed or superseded outputs."""
    manifests = provenance.load_call_manifests(round_dir)
    scope_path = round_dir / "round-scope.json"
    scope = json.loads(scope_path.read_text(encoding="utf-8")) if scope_path.is_file() else {}
    targets = set()
    source = LiveSource()
    for manifest in manifests:
        for record in manifest.get("outputs") or []:
            targets.add((manifest["step"], manifest.get("persona"),
                         source.output_key(record.get("path", ""), round_verify._root(manifest))))
    differences = []
    for step, persona, output in sorted(targets, key=lambda item: (item[0], item[1] or "", item[2])):
        kwargs = {"round_dir": round_dir, "step": step, "persona": persona,
                  "output_path": Path(output), "registered_models": scope.get("models", [])}
        old = _capture(legacy.reconcile_artifact, **kwargs)
        new = _capture(provenance.reconcile_artifact, **kwargs)
        if old != new:
            differences.append({"round": str(round_dir), "output": output,
                                "step": step, "persona": persona, "legacy": old, "current": new})
    return differences


def run_regression(repo: Path, report_dir: Path, *, include_extended: bool = False,
                   window_confirmed: str | None = None) -> int:
    """Refuse internal destinations, unconfirmed Extended access, census drift and omissions."""
    repo = repo.resolve(strict=True)
    if repo.is_relative_to("/Volumes"):
        raise RegisterError(f"refused regression repo {repo}: use a boot repository")
    report_dir = report_dir.resolve()
    if report_dir.is_relative_to(repo):
        raise RegisterError(f"refused --report-dir {report_dir}: resolves inside repo {repo}")
    if include_extended and not window_confirmed:
        raise RegisterError("refused --include-extended: --window-confirmed REF required")
    if window_confirmed is not None and (not window_confirmed.strip()
                                         or any(c in window_confirmed for c in "\t\r\n\0")):
        raise RegisterError(f"refused --window-confirmed {window_confirmed!r}: invalid reference")
    if report_dir.is_relative_to("/Volumes") and not window_confirmed:
        raise RegisterError(f"refused --report-dir {report_dir}: --window-confirmed REF required")
    entries = discover(repo, include_extended=include_extended)
    rounds = []
    for audit, entry in sorted(entries.items()):
        _component(audit, "audit_id")
        for directory in sorted(entry["rounds"]):
            _component(directory.name, "round name")
            if report_dir.is_relative_to(directory.resolve()):
                raise RegisterError(f"refused --report-dir {report_dir}: inside round {directory}")
            rounds.append((audit, directory))
    if len({(audit, directory.name) for audit, directory in rounds}) != len(rounds):
        raise RegisterError("refused regression rounds: duplicate report paths")
    before = {directory: lstat_census(directory) for _, directory in rounds}
    report_dir.mkdir(parents=True, exist_ok=True)
    if any(report_dir.iterdir()):
        raise RegisterError(f"refused --report-dir {report_dir}: must be empty")
    counts = {mode: Counter() for mode in ("live", "archive")}
    summary = {"include_extended": include_extended, "window_confirmed": window_confirmed,
               "rounds_total": len(rounds), "counts": {}, "t1_differences": [],
               "raised": [], "round_changes": [], "rounds_accounted": {}}

    def raised(directory: Path, stage: str, exc: BaseException, stderr: str = "") -> None:
        summary["raised"].append({"round": str(directory), "stage": stage,
                                  "exception": type(exc).__name__, "reason": str(exc), "stderr": stderr})

    try:
        for audit, directory in rounds:
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    summary["t1_differences"].extend(compare_legacy(directory))
            except (Exception, SystemExit) as exc:
                raised(directory, "t1", exc, err.getvalue())
            destination = report_dir / audit
            destination.mkdir(exist_ok=True)
            for mode in ("live", "archive"):
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err):
                        round_verify.verify_round(directory, mode=mode,
                                                  json_out=destination / f"{directory.name}.{mode}.json",
                                                  remaps=[f"{repo}={repo}"] if mode == "archive" else None)
                    verdict = json.loads((destination / f"{directory.name}.{mode}.json").read_text())
                    counts[mode][verdict["verdict"]] += 1
                except (Exception, SystemExit) as exc:
                    raised(directory, mode, exc, err.getvalue())
    finally:
        for _, directory in rounds:
            try:
                after = lstat_census(directory)
                changes = sorted(key for key in before[directory].keys() | after.keys()
                                 if before[directory].get(key) != after.get(key))
                if changes:
                    summary["round_changes"].append({"round": str(directory), "paths": changes})
            except OSError as exc:
                raised(directory, "lstat-after", exc)
    summary["counts"] = {mode: dict(sorted(counts[mode].items())) for mode in counts}
    summary["rounds_accounted"] = {
        mode: sum(counts[mode].values()) + sum(item["stage"] == mode for item in summary["raised"])
        for mode in counts
    }
    summary["raised"].sort(key=lambda item: (item["round"], item["stage"]))
    provenance.write_json_once(report_dir / "summary.json", summary)
    return int(bool(summary["t1_differences"] or summary["raised"] or summary["round_changes"]
                    or any(total != len(rounds) for total in summary["rounds_accounted"].values())))


def main(argv: list[str] | None = None) -> int:
    """Refuse unsafe report or window arguments; leave all repository evidence read-only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--include-extended", action="store_true")
    parser.add_argument("--window-confirmed")
    args = parser.parse_args(argv)
    try:
        return run_regression(args.repo, args.report_dir, include_extended=args.include_extended,
                              window_confirmed=args.window_confirmed)
    except (OSError, ValueError, RegisterError) as exc:
        sys.stderr.write(f"regression: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
