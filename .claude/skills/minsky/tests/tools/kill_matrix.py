#!/usr/bin/env python3
"""Run fixture-only mutations; refuse dirty baselines, ambiguous patches and unintended kills."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True

SKILL_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import provenance

REPO_ROOT = SKILL_DIR.parents[2]
PY = sys.executable
SKILL_REL = Path(".claude/skills/minsky")
FIELDS = {"id", "file", "old", "new", "description", "expected_killers"}
HOOK_REL = Path("scripts/git-hooks/pre-commit")
DEFERRED = []


class MatrixError(RuntimeError):
    """A matrix input or fixture operation was refused."""


def load_mutants(path: Path) -> list[dict]:
    """Refuse malformed entries, duplicate ids and paths escaping the skill copy."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise MatrixError(f"refused mutants {path}: expected a non-empty JSON array")
    ids = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise MatrixError(f"refused mutant {entry!r}: expected fields {sorted(FIELDS)}")
        for field in FIELDS - {"expected_killers"}:
            if not isinstance(entry[field], str) or (field != "new" and not entry[field]):
                raise MatrixError(f"refused mutant {entry.get('id')!r}: invalid {field}")
        name = PurePosixPath(entry["file"])
        if (name.is_absolute() or ".." in name.parts or name.as_posix() != entry["file"]
                or any(c in entry["file"] for c in "\\\r\n\0") or name == PurePosixPath(".")):
            raise MatrixError(f"refused mutant {entry['id']}: unsafe file {entry['file']!r}")
        killers = entry["expected_killers"]
        if not isinstance(killers, list) or not killers or any(not isinstance(k, str) or not k for k in killers):
            raise MatrixError(f"refused mutant {entry['id']}: expected_killers must be non-empty prefixes")
        for killer in killers:
            test = PurePosixPath(killer.partition("::")[0])
            if (not test.is_relative_to(PurePosixPath(SKILL_REL.as_posix()) / "tests")
                    or ".." in test.parts or test.suffix != ".py"
                    or test.as_posix() != killer.partition("::")[0]
                    or any(c in killer for c in "\\\r\n\0")):
                raise MatrixError(f"refused mutant {entry['id']}: unsafe expected killer {killer!r}")
        if entry["id"] in ids:
            raise MatrixError(f"refused duplicate mutant id {entry['id']!r}")
        ids.add(entry["id"])
    return sorted(value, key=lambda entry: entry["id"])


def check_catalogue(mutants: list[dict]) -> None:
    """Refuse missing or duplicate guard deletions and missing plan cases."""
    prefix = "delete any single guard: "
    deletions = Counter(entry["description"][len(prefix):] for entry in mutants
                        if entry["id"].startswith("T1-") and entry["file"] == "scripts/provenance.py"
                        and entry["description"].startswith(prefix))
    reasons = [f"guard {guard}: expected exactly one deletion mutant, found {deletions[guard]}"
               for guard in provenance.GUARDS if deletions[guard] != 1]
    reasons.extend(f"unknown guard deletion {guard}" for guard in sorted(deletions.keys() - set(provenance.GUARDS)))
    cases = {entry["id"].partition("-")[0] for entry in mutants}
    reasons.extend(f"missing T{number} mutant" for number in range(2, 25)
                   if f"T{number}" not in cases)
    if reasons:
        raise MatrixError("refused mutant catalogue: " + "; ".join(reasons))


def _interpreter() -> str:
    """Refuse an interpreter override that is not an absolute executable file."""
    value = os.environ.get("MINSKY_PYTHON", PY)
    if not Path(value).is_absolute() or not Path(value).is_file() or not os.access(value, os.X_OK):
        raise MatrixError(f"refused MINSKY_PYTHON {value!r}: an absolute executable file is required")
    return value


def _environment(test_root: Path) -> dict[str, str]:
    # The copied conftest empties its entire test root. Copies live beside this nested root, not inside it.
    env = {**os.environ, "MINSKY_TEST_TMP": str(test_root), "GIT_OPTIONAL_LOCKS": "0",
           "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "minsky-fixture", "GIT_COMMITTER_NAME": "minsky-fixture",
           "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
           "GIT_AUTHOR_DATE": "2026-10-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-10-01T00:00:00Z"}
    for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_COUNT"):
        env.pop(key, None)
    return env


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(repo),
                             "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                            env=env, capture_output=True, text=True)
    if result.returncode:
        raise MatrixError(f"refused fixture git {' '.join(args)}: {result.stderr.strip()}")


def _production_source(skill: Path) -> Path | None:
    """Refuse malformed config or escaping paths; null explicitly needs no production file."""
    config = skill / "config/free_space.json"
    try:
        value = json.loads(config.read_bytes())
    except (OSError, ValueError, RecursionError) as exc:
        raise MatrixError(f"refused free-space config {config}: {exc}") from exc
    if not isinstance(value, dict) or "production_floor_source" not in value:
        raise MatrixError(f"refused free-space config {config}: missing production_floor_source")
    source = value["production_floor_source"]
    if source is None:
        return None
    relative = source.get("path") if isinstance(source, dict) else None
    if not isinstance(relative, str) or not relative:
        raise MatrixError(f"refused free-space config {config}: invalid production_floor_source {source!r}")
    path = PurePosixPath(relative)
    if (path.is_absolute() or ".." in path.parts or ".git" in path.parts or path == PurePosixPath(".")
            or path.as_posix() != relative or any(c in relative for c in "\\\r\n\0")):
        raise MatrixError(f"refused free-space config {config}: unsafe production_floor_source path {relative!r}")
    return Path(relative)


def _copy(skill: Path, repo_root: Path, repo: Path) -> Path:
    copied = repo / SKILL_REL
    shutil.copytree(skill, copied, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    hooks = tuple(path.relative_to(repo_root) for path in sorted((repo_root / HOOK_REL.parent).rglob("*"))
                  if path.is_file() and path.relative_to(repo_root) != HOOK_REL)
    production = _production_source(skill)
    external = (Path("pytest.ini"), HOOK_REL, *hooks)
    if production is not None:
        external = (production, *external)
    for relative in external:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo_root / relative, target)
    if (repo_root / "pytest.ini").read_bytes() != (repo / "pytest.ini").read_bytes():
        raise MatrixError("refused fixture copy pytest.ini: byte mismatch")
    pairs = [(skill / path.relative_to(copied), path) for path in sorted(copied.rglob("*"))
             if not path.is_symlink() and path.is_file()]
    pairs.extend((repo_root / relative, repo / relative) for relative in external)
    for source, target in pairs:
        if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
            raise MatrixError(f"refused fixture copy {target.relative_to(repo)}: sha256 mismatch")
    return copied


def _junit(path: Path, copied_tests: Path) -> tuple[list[str], bool]:
    root = ET.parse(path).getroot()
    errors = root.findall(".//error")
    test_errors = []
    failed = []
    for case in root.iter("testcase"):
        error = case.find("error")
        if case.find("failure") is None and error is None:
            continue
        if error is not None and (not case.get("classname") or error.get("message") == "collection failure"):
            continue
        classname = case.get("classname", "").split(".")
        matches = []
        for source in copied_tests.rglob("test_*.py"):
            if source.stem in classname:
                index = classname.index(source.stem)
                suffix = classname[index + 1:] + [case.get("name", "")]
                matches.append((SKILL_REL / "tests" / source.relative_to(copied_tests)).as_posix()
                               + "::" + "::".join(suffix))
        if len(matches) != 1:
            raise MatrixError(f"refused JUnit failing test {case.get('classname')}::{case.get('name')}: ambiguous node id")
        failed.append(matches[0])
        # Host fix (Astra W9c): a test case can carry several <error> children (setup AND teardown); all belong to it.
        test_errors.extend(case.findall("error"))
    return sorted(set(failed)), any(error not in test_errors for error in errors)


def _trial(skill: Path, repo_root: Path, temp_root: Path, mutant: dict | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="w9-matrix-", suffix=".noindex", dir=temp_root) as scratch:
        workspace = Path(scratch)
        repo = workspace / "repo"
        env = _environment(workspace / "child-0")
        copied = _copy(skill, repo_root, repo)
        _git(repo, env, "init", "-q")
        production = _production_source(skill)
        paths = [str(SKILL_REL), "pytest.ini", str(HOOK_REL.parent)]
        if production is not None:
            paths.append(str(production))
        _git(repo, env, "add", "--", *paths)
        _git(repo, env, "commit", "-q", "-m", "W9 fixture baseline")
        if mutant is not None:
            hook = mutant["file"] == HOOK_REL.as_posix()
            target = repo / HOOK_REL if hook else copied / mutant["file"]
            boundary = repo if hook else copied
            if target.is_symlink() or not target.resolve().is_relative_to(boundary.resolve()):
                return {"status": "invalid-mutant", "killing_tests": [], "reason": "patch target escapes allowed copy or is a symlink"}
            try:
                original = target.read_text(encoding="utf-8")
                count = original.count(mutant["old"])
                if count != 1:
                    return {"status": "invalid-mutant", "killing_tests": [], "reason": f"old text occurs {count} times"}
                updated = original.replace(mutant["old"], mutant["new"], 1)
                if target.suffix == ".py":
                    compile(updated, mutant["file"], "exec")
                target.write_text(updated, encoding="utf-8")
            except (OSError, UnicodeError, SyntaxError) as exc:
                return {"status": "invalid-mutant", "killing_tests": [], "reason": type(exc).__name__}
        runs = 0

        def pytest_run(selectors: list[str], *, collect: bool = False) -> tuple[subprocess.CompletedProcess[str], Path]:
            nonlocal runs
            tests_root = workspace / f"child-{runs}"
            tests_root.mkdir()
            junit = workspace / f"junit-{runs}.xml"
            runs += 1
            argv = [_interpreter(), "-m", "pytest", *selectors, "-q", "-p", "no:cacheprovider",
                    f"--basetemp={tests_root / 'bt'}", f"--junitxml={junit}"]
            if collect:
                argv.append("--collect-only")
            return subprocess.run(argv, cwd=repo, env=_environment(tests_root),
                                  capture_output=True, text=True), junit

        def verdict(result: subprocess.CompletedProcess[str], junit: Path, *, focused: bool) -> dict:
            try:
                failed, errors = _junit(junit, copied / "tests")
            except (OSError, ET.ParseError, MatrixError):
                reason = "missing or invalid expected killer JUnit report" if focused else "missing or invalid JUnit report"
                return {"status": "invalid-mutant", "killing_tests": [], "reason": reason}
            if errors:
                reason = "expected killer collection failed" if focused else "pytest collection failed"
                return {"status": "invalid-mutant", "killing_tests": failed, "reason": reason}
            if result.returncode == 0 and not failed:
                return {"status": "survived", "killing_tests": []}
            if result.returncode == 1 and failed:
                return {"status": "killed" if focused else "killed-unexpected", "killing_tests": failed}
            return {"status": "invalid-mutant", "killing_tests": failed,
                    "reason": "pytest errors or non-test failure"}

        if mutant is None:
            result, junit = pytest_run([str(copied / "tests")])
            if result.returncode:
                sys.stderr.write("kill-matrix: refused unmutated fixture baseline\n" + result.stdout + result.stderr)
            baseline = verdict(result, junit, focused=False)
            if baseline["status"] == "survived":
                cases = list(ET.parse(junit).getroot().iter("testcase"))
                skipped = sum(case.find("skipped") is not None for case in cases)
                return {"status": "survived", "killing_tests": [],
                        "tests_passed": len(cases) - skipped, "tests_skipped": skipped}
            return baseline

        expected = mutant["expected_killers"]
        files = sorted({prefix.partition("::")[0] for prefix in expected})
        collected, _ = pytest_run(files, collect=True)
        if collected.returncode:
            return {"status": "invalid-mutant", "killing_tests": [], "reason": "expected killer collection failed"}
        nodes = sorted({node for node in collected.stdout.splitlines()
                        if any(node.startswith(prefix) for prefix in expected)})
        if not nodes:
            return {"status": "invalid-mutant", "killing_tests": [],
                    "reason": f"expected killers collected no tests: {expected}"}
        result, junit = pytest_run(nodes)
        intended = verdict(result, junit, focused=True)
        if intended["status"] != "survived":
            return intended
        result, junit = pytest_run([str(copied / "tests")])
        return verdict(result, junit, focused=False)


def run_matrix(mutants_path: Path, out: Path, *, only: list[str] | None = None,
               skill_dir: Path = SKILL_DIR, repo_root: Path = REPO_ROOT) -> int:
    """Refuse unsafe temp roots and bad baselines; clean each copy even for invalid mutations."""
    mutants = load_mutants(mutants_path)
    check_catalogue(mutants)
    interpreter = _interpreter()
    if only is not None:
        unknown = set(only) - {entry["id"] for entry in mutants}
        if unknown:
            raise MatrixError(f"refused --only unknown mutant ids: {sorted(unknown)}")
        mutants = [entry for entry in mutants if entry["id"] in only]
    value = os.environ.get("MINSKY_TEST_TMP")
    if not value or not Path(value).is_absolute():
        raise MatrixError(f"refused MINSKY_TEST_TMP {value!r}: an absolute test root is required")
    temp_root = Path(value).resolve()
    if (skill_dir.resolve().is_relative_to(temp_root) or Path.home().resolve().is_relative_to(temp_root)
            or temp_root.is_relative_to(skill_dir.resolve()) or temp_root == Path(temp_root.anchor)):
        raise MatrixError(f"refused MINSKY_TEST_TMP {temp_root}: contains home or skill, or is inside skill")
    if out.exists() or out.is_symlink():
        raise MatrixError(f"refused matrix output {out}: already exists")
    temp_root.mkdir(parents=True, exist_ok=True)
    baseline = _trial(skill_dir, repo_root, temp_root, None)
    report = {"baseline": {**baseline, "status": "passed" if baseline["status"] == "survived" else "invalid",
                           "interpreter": interpreter},
              "mutants": [], "deferred": DEFERRED}
    def trial(entry: dict) -> dict:
        result = _trial(skill_dir, repo_root, temp_root, entry)
        result.update(id=entry["id"], file=entry["file"],
                      patch_sha256=hashlib.sha256((entry["old"] + entry["new"]).encode("utf-8")).hexdigest(),
                      expected_killer=any(node.startswith(prefix) for node in result["killing_tests"]
                                          for prefix in entry["expected_killers"]))
        return result

    if baseline["status"] == "survived":
        with ThreadPoolExecutor(max_workers=4) as pool:
            report["mutants"] = list(pool.map(trial, mutants))
    with out.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["baseline"]["status"] != "passed":
        return 2
    return int(any(row["status"] != "killed" or not row["expected_killer"] for row in report["mutants"]))


def main(argv: list[str] | None = None) -> int:
    """Refuse malformed matrix arguments; a surviving, invalid or unintended mutant exits one."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutants", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="+", action="extend")
    args = parser.parse_args(argv)
    path = args.mutants
    if not path.is_absolute() and not path.exists():
        path = SKILL_DIR / path
    try:
        return run_matrix(path, args.out, only=args.only)
    except (OSError, ValueError, MatrixError) as exc:
        sys.stderr.write(f"kill-matrix: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
