"""Exercise W9 on KB fixtures; refuse false kills, census omissions and repository writes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from test_hardening import PERSONA, record_call, register_round
from lifecycle_register import RegisterError

TOOLS = Path(__file__).resolve().parent / "tools"
CATALOGUE = TOOLS.parent / "mutants.json"
TINY_IDS = ["T2-killed", "T3-survived", "T4-invalid"]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix = _load("w9_matrix", TOOLS / "kill_matrix.py")
regression = _load("w9_regression", TOOLS / "regression.py")


def _write(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _tiny(tmp: Path) -> tuple[Path, Path, Path]:
    repo = tmp / "tiny-repo"
    skill = repo / ".claude/skills/minsky"
    _write(skill / "scripts/probe.py", "VALUE = 1\nUNUSED = 0\n")
    _write(skill / "tests/test_probe.py", (
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))\n"
        "import probe\n\ndef test_value():\n    assert probe.VALUE == 1\n"
    ))
    _write(repo / "scripts/r3_continuous.py", "MIN_FREE_BYTES = 5 * 2**30\n")
    _write(skill / "config/free_space.json", json.dumps({
        "production_floor_source": {"path": "scripts/r3_continuous.py", "name": "MIN_FREE_BYTES"},
    }))
    _write(repo / "pytest.ini", "[pytest]\nnorecursedirs = tools\n")
    _write(repo / "scripts/git-hooks/pre-commit", "#!/bin/sh\nexit 0\n").chmod(0o755)
    _write(repo / "scripts/git-hooks/pre-merge-commit", "#!/bin/sh\n# merge fixture\nexit 0\n").chmod(0o751)
    entries = [
        {"id": "T2-killed", "file": "scripts/probe.py", "old": "VALUE = 1", "new": "VALUE = 2",
         "description": "fixture killed", "expected_killers": [".claude/skills/minsky/tests/test_probe.py::test_value"]},
        {"id": "T3-survived", "file": "scripts/probe.py", "old": "UNUSED = 0", "new": "UNUSED = 1",
         "description": "fixture survived", "expected_killers": [".claude/skills/minsky/tests/test_probe.py::test_value"]},
        {"id": "T4-invalid", "file": "scripts/probe.py", "old": "VALUE = 1",
         "new": "raise RuntimeError('collection mutant')\nVALUE = 1",
         "description": "fixture collection error", "expected_killers": [".claude/skills/minsky/tests/test_probe.py::test_value"]},
    ]
    path = _write(tmp / "mutants.json", json.dumps(entries + json.loads(CATALOGUE.read_text())))
    return repo, skill, path


def test_kill_matrix_killed_survived_invalid_and_cleanup(tmp_path: Path, monkeypatch) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    root = tmp_path / "matrix-temp"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    out = tmp_path / "matrix.json"
    assert matrix.run_matrix(mutants, out, only=TINY_IDS, skill_dir=skill, repo_root=repo) == 1
    report = json.loads(out.read_text())
    assert report["baseline"]["status"] == "passed"
    assert report["baseline"]["tests_passed"] == 1 and report["baseline"]["tests_skipped"] == 0
    assert report["deferred"] == []
    assert report["baseline"]["interpreter"] == os.environ.get("MINSKY_PYTHON", matrix.PY)
    rows = {row["id"]: row for row in report["mutants"]}
    assert {name: row["status"] for name, row in rows.items()} == {
        "T2-killed": "killed", "T3-survived": "survived", "T4-invalid": "invalid-mutant",
    }
    killed = rows["T2-killed"]
    assert killed["killing_tests"] == [".claude/skills/minsky/tests/test_probe.py::test_value"]
    assert killed["expected_killer"] is True
    assert killed["patch_sha256"] == hashlib.sha256(b"VALUE = 1VALUE = 2").hexdigest()
    assert rows["T4-invalid"]["killing_tests"] == []
    assert list(root.iterdir()) == []
    repeat = tmp_path / "repeat.json"
    assert matrix.run_matrix(mutants, repeat, only=TINY_IDS, skill_dir=skill, repo_root=repo) == 1
    assert out.read_bytes() == repeat.read_bytes()
    assert list(root.iterdir()) == []


def test_kill_matrix_invalid_baseline_exits_two_and_cleans(tmp_path: Path, monkeypatch) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    (skill / "scripts/probe.py").write_text("VALUE = 9\nUNUSED = 0\n")
    root = tmp_path / "matrix-temp"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    out = tmp_path / "baseline.json"
    assert matrix.run_matrix(mutants, out, only=TINY_IDS, skill_dir=skill, repo_root=repo) == 2
    report = json.loads(out.read_text())
    assert report["baseline"]["status"] == "invalid" and report["mutants"] == []
    assert list(root.iterdir()) == []


def test_kill_matrix_only_and_unexpected_killer_refuses(tmp_path: Path, monkeypatch) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    with (skill / "tests/test_probe.py").open("a") as stream:
        stream.write("\ndef test_other():\n    assert probe.UNUSED == 0\n")
    entries = json.loads(mutants.read_text())
    entries[0]["expected_killers"] = [".claude/skills/minsky/tests/test_probe.py::test_other"]
    mutants.write_text(json.dumps(entries))
    root = tmp_path / "matrix-temp"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    out = tmp_path / "unexpected.json"
    assert matrix.run_matrix(mutants, out, only=["T2-killed"], skill_dir=skill, repo_root=repo) == 1
    rows = json.loads(out.read_text())["mutants"]
    assert len(rows) == 1 and rows[0]["status"] == "killed-unexpected" and rows[0]["expected_killer"] is False
    assert rows[0]["killing_tests"] == [".claude/skills/minsky/tests/test_probe.py::test_value"]
    with pytest.raises(matrix.MatrixError, match="unknown mutant ids"):
        matrix.run_matrix(mutants, tmp_path / "unknown.json", only=["missing"], skill_dir=skill, repo_root=repo)
    assert list(root.iterdir()) == []


def test_kill_matrix_ambiguous_patch_and_cleanup_on_error(tmp_path: Path, monkeypatch) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    entries = json.loads(mutants.read_text())
    entries[0]["old"] = "not present"
    mutants.write_text(json.dumps(entries))
    root = tmp_path / "matrix-temp"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    out = tmp_path / "ambiguous.json"
    assert matrix.run_matrix(mutants, out, only=["T2-killed"], skill_dir=skill, repo_root=repo) == 1
    row = json.loads(out.read_text())["mutants"][0]
    assert row["status"] == "invalid-mutant" and row["reason"] == "old text occurs 0 times"
    monkeypatch.setattr(matrix.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("fixture spawn")))
    with pytest.raises(OSError, match="fixture spawn"):
        matrix.run_matrix(mutants, tmp_path / "error.json", only=TINY_IDS, skill_dir=skill, repo_root=repo)
    assert list(root.iterdir()) == []


def _rounds(tmp: Path) -> tuple[Path, list[Path]]:
    repo = tmp / "fixture-repo"
    rounds = [repo / "audits/test-audit" / name for name in ("round-2", "round-3")]
    for index, directory in enumerate(rounds):
        register_round(directory)
        scope = directory / "round-scope.json"
        value = json.loads(scope.read_text())
        value["schema_version"] = "1.0"
        scope.write_text(json.dumps(value))
        output = directory / "result.json"
        output.write_text("{}\n")
        record_call(directory, uid=f"w9call00{index}", step="codex", persona=PERSONA,
                    model="gpt-5.6-sol", effort="medium", outputs=[output],
                    invoked_at="2026-10-01T00:00:00Z", usage={"status": "total-only", "total_tokens": 1})
    (rounds[1] / "result.json").write_text("changed\n")
    return repo, rounds


def test_regression_two_rounds_verdicts_counts_identity_and_no_writes(tmp_path: Path) -> None:
    repo, rounds = _rounds(tmp_path)
    before = {root: regression.lstat_census(root) for root in rounds}
    out = tmp_path / "reports"
    assert regression.main(["--repo", str(repo), "--report-dir", str(out)]) == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["rounds_total"] == 2
    assert summary["rounds_accounted"] == {"live": 2, "archive": 2}
    assert summary["t1_differences"] == summary["raised"] == summary["round_changes"] == []
    assert summary["include_extended"] is False and summary["window_confirmed"] is None
    for mode, clean in (("live", "ok-live"), ("archive", "consistent-unanchored")):
        assert sum(summary["counts"][mode].values()) == 2
        first = json.loads((out / "test-audit" / f"round-2.{mode}.json").read_text())
        second = json.loads((out / "test-audit" / f"round-3.{mode}.json").read_text())
        assert first["verdict"] == clean and second["verdict"].startswith("fail:output hash mismatch:")
        if mode == "archive":
            assert first["remaps"] == [[str(repo), str(repo)]]
            assert first["remap_log"] and first["store"] is first["pack"] is None
    assert {root: regression.lstat_census(root) for root in rounds} == before


@pytest.mark.parametrize("symlink", [False, True])
def test_regression_internal_out_refused(tmp_path: Path, symlink: bool, capsys) -> None:
    repo, _ = _rounds(tmp_path)
    out = repo / "reports"
    if symlink:
        link = tmp_path / "report-link"
        link.symlink_to(repo, target_is_directory=True)
        out = link / "reports"
    assert regression.main(["--repo", str(repo), "--report-dir", str(out)]) == 1
    assert "resolves inside repo" in capsys.readouterr().err
    assert not out.exists()


def test_regression_t1_differences_and_raised_rounds_are_counted(tmp_path: Path, monkeypatch) -> None:
    repo, rounds = _rounds(tmp_path)
    reconcile = regression.provenance.reconcile_artifact

    def changed(*args, **kwargs):
        ok, _, manifest = reconcile(*args, **kwargs)
        return ok, "fixture differential", manifest

    monkeypatch.setattr(regression.provenance, "reconcile_artifact", changed)
    verify = regression.round_verify.verify_round

    def raises(directory, **kwargs):
        if directory == rounds[1] and kwargs["mode"] == "live":
            raise RuntimeError("fixture live failure")
        return verify(directory, **kwargs)

    monkeypatch.setattr(regression.round_verify, "verify_round", raises)
    out = tmp_path / "reports"
    assert regression.run_regression(repo, out) == 1
    summary = json.loads((out / "summary.json").read_text())
    assert len(summary["t1_differences"]) == 2
    assert summary["rounds_accounted"] == {"live": 2, "archive": 2}
    assert sum(summary["counts"]["live"].values()) == 1
    assert summary["raised"] == [{"round": str(rounds[1]), "stage": "live", "exception": "RuntimeError",
                                  "reason": "fixture live failure", "stderr": ""}]
    assert (out / "test-audit/round-3.archive.json").is_file()


def test_regression_round_mutation_detected(tmp_path: Path, monkeypatch) -> None:
    repo, rounds = _rounds(tmp_path)
    verify = regression.round_verify.verify_round

    def changed(directory, **kwargs):
        result = verify(directory, **kwargs)
        if directory == rounds[0] and kwargs["mode"] == "live":
            (directory / "unexpected").write_text("fixture mutation")
        return result

    monkeypatch.setattr(regression.round_verify, "verify_round", changed)
    out = tmp_path / "reports"
    assert regression.run_regression(repo, out) == 1
    changes = json.loads((out / "summary.json").read_text())["round_changes"]
    assert changes[0]["round"] == str(rounds[0]) and "unexpected" in changes[0]["paths"]


def test_regression_unconfirmed_extended_refused_before_discovery(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    monkeypatch.setattr(regression, "discover", lambda *a, **k: pytest.fail("unauthorized discovery"))
    with pytest.raises(RegisterError, match="window-confirmed REF required"):
        regression.run_regression(repo, tmp_path / "reports", include_extended=True)


def test_regression_discovery_does_not_follow_symlinks(tmp_path: Path) -> None:
    repo, rounds = _rounds(tmp_path)
    (repo / "deferred-audit").symlink_to("/Volumes/W9_UNREAD_FIXTURE", target_is_directory=True)
    found = regression.discover(repo, include_extended=False)
    assert found == {"test-audit": {"audit_dir": rounds[0].parent, "rounds": rounds}}


def test_kill_matrix_junit_preserves_parametrized_node_ids(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    _write(tests / "test_probe.py", "")
    xml = _write(tmp_path / "junit.xml", (
        '<testsuites><testsuite><testcase classname=".claude.skills.minsky.tests.test_probe.TestProbe" '
        'name="test_value[guard-case]"><failure message="fixture"/></testcase></testsuite></testsuites>'
    ))
    assert matrix._junit(xml, tests) == ([
        ".claude/skills/minsky/tests/test_probe.py::TestProbe::test_value[guard-case]",
    ], False)


def test_kill_matrix_junit_collection_error_is_not_a_test_failure(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    xml = _write(tmp_path / "junit.xml", (
        '<testsuites><testsuite><testcase name="collection"><error message="import failure"/>'
        '</testcase></testsuite></testsuites>'
    ))
    assert matrix._junit(xml, tests) == ([], True)


def test_kill_matrix_junit_setup_and_teardown_errors_are_killers(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    _write(tests / "test_probe.py", "")
    xml = _write(tmp_path / "junit.xml", (
        '<testsuites><testsuite>'
        '<testcase classname="test_probe" name="test_setup[case]">'
        '<error message="failed on setup with fixture"/></testcase>'
        '<testcase classname="test_probe" name="test_teardown">'
        '<error message="failed on teardown with fixture"/></testcase>'
        '</testsuite></testsuites>'
    ))
    assert matrix._junit(xml, tests) == ([
        ".claude/skills/minsky/tests/test_probe.py::test_setup[case]",
        ".claude/skills/minsky/tests/test_probe.py::test_teardown",
    ], False)


def test_kill_matrix_junit_collection_error_overrides_named_failure(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    _write(tests / "test_probe.py", "")
    xml = _write(tmp_path / "junit.xml", (
        '<testsuites><testsuite>'
        '<testcase classname="test_probe" name="test_value"><failure message="fixture"/></testcase>'
        '<testcase classname="test_probe" name="collection"><error message="collection failure"/></testcase>'
        '</testsuite></testsuites>'
    ))
    assert matrix._junit(xml, tests) == ([".claude/skills/minsky/tests/test_probe.py::test_value"], True)


def test_kill_matrix_temp_root_refuses_relative(tmp_path: Path, monkeypatch) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    monkeypatch.setenv("MINSKY_TEST_TMP", "relative")
    with pytest.raises(matrix.MatrixError, match="absolute test root"):
        matrix.run_matrix(mutants, tmp_path / "report.json", skill_dir=skill, repo_root=repo)
    assert os.environ["MINSKY_TEST_TMP"] == "relative"


@pytest.mark.parametrize("guard", matrix.provenance.GUARDS)
def test_kill_matrix_missing_guard_refused_before_trials(tmp_path: Path, monkeypatch, capsys,
                                                       guard: str) -> None:
    entries = [entry for entry in json.loads(CATALOGUE.read_text())
               if entry["description"] != f"delete any single guard: {guard}"]
    mutants = _write(tmp_path / "mutants.json", json.dumps(entries))
    monkeypatch.setattr(matrix, "_trial", lambda *a, **k: pytest.fail("incomplete catalogue ran a trial"))
    out = tmp_path / "matrix.json"
    assert matrix.main(["--mutants", str(mutants), "--out", str(out)]) == 2
    assert f"guard {guard}: expected exactly one deletion mutant, found 0" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("number", range(2, 25))
def test_kill_matrix_missing_plan_case_refused_before_only(tmp_path: Path, monkeypatch, capsys,
                                                         number: int) -> None:
    entries = [entry for entry in json.loads(CATALOGUE.read_text())
               if not entry["id"].startswith(f"T{number}-")]
    mutants = _write(tmp_path / "mutants.json", json.dumps(entries))
    monkeypatch.setattr(matrix, "_trial", lambda *a, **k: pytest.fail("incomplete catalogue ran a trial"))
    out = tmp_path / "matrix.json"
    assert matrix.main(["--mutants", str(mutants), "--out", str(out),
                        "--only", "T1-round-scope-loads"]) == 2
    assert f"missing T{number} mutant" in capsys.readouterr().err
    assert not out.exists()


def test_kill_matrix_duplicate_guard_and_deferred_case_refused() -> None:
    entries = json.loads(CATALOGUE.read_text())
    matrix.check_catalogue(entries)
    duplicate = {**entries[0], "id": "T1-duplicate-guard"}
    with pytest.raises(matrix.MatrixError, match="audit_round_match.*found 2"):
        matrix.check_catalogue(entries + [duplicate])
    unknown = {**duplicate, "description": "delete any single guard: unknown_guard"}
    with pytest.raises(matrix.MatrixError, match="unknown guard deletion unknown_guard"):
        matrix.check_catalogue(entries + [unknown])
    matrix.check_catalogue(entries + [{**duplicate, "id": "T23-accepted-publish-check"}])


def test_w10c_production_source_configured_is_copied_and_added(tmp_path: Path) -> None:
    repo, skill, _ = _tiny(tmp_path)
    relative = Path("production/fixture_floor.py")
    source = _write(repo / relative, "MIN_FREE_BYTES = 5 * 2**30\n")
    (repo / "scripts/r3_continuous.py").unlink()
    _write(skill / "config/free_space.json", json.dumps({
        "production_floor_source": {"path": relative.as_posix(), "name": "MIN_FREE_BYTES"},
    }))
    assert matrix._production_source(skill) == relative
    target = tmp_path / "copied-repo"
    matrix._copy(skill, repo, target)
    assert (target / relative).read_bytes() == source.read_bytes()
    assert not (target / "scripts/r3_continuous.py").exists()
    _write(skill / "tests/test_source.py", (
        "import subprocess\nfrom pathlib import Path\n\n"
        "def test_source():\n"
        "    repo = Path(__file__).resolve().parents[4]\n"
        f"    relative = {relative.as_posix()!r}\n"
        f"    assert (repo / relative).read_bytes() == {source.read_bytes()!r}\n"
        "    assert subprocess.run(['git', '--no-optional-locks', '-C', str(repo), 'ls-files', "
        "'--error-unmatch', '--', relative], capture_output=True).returncode == 0\n"
    ))
    root = tmp_path / "matrix-temp"
    root.mkdir()
    assert matrix._trial(skill, repo, root, None) == {
        "status": "survived", "killing_tests": [], "tests_passed": 2, "tests_skipped": 0,
    }
    assert list(root.iterdir()) == []


def test_w10c_production_source_null_requires_no_production_file(tmp_path: Path) -> None:
    repo, skill, _ = _tiny(tmp_path)
    _write(skill / "config/free_space.json", '{"production_floor_source": null}\n')
    (repo / "scripts/r3_continuous.py").unlink()
    assert matrix._production_source(skill) is None
    target = tmp_path / "copied-repo"
    matrix._copy(skill, repo, target)
    assert not (target / "scripts/r3_continuous.py").exists()
    root = tmp_path / "matrix-temp"
    root.mkdir()
    assert matrix._trial(skill, repo, root, None) == {
        "status": "survived", "killing_tests": [], "tests_passed": 1, "tests_skipped": 0,
    }
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("payload", [
    None, "{broken", "[]", "{}", '{"production_floor_source": false}',
    '{"production_floor_source": 1}', '{"production_floor_source": "source.py"}',
    '{"production_floor_source": []}', '{"production_floor_source": {}}',
    *[json.dumps({"production_floor_source": {"path": path}})
      for path in (None, 1, "/outside.py", "", "..", "../outside.py", "scripts/../source.py", ".",
                   ".git", ".git/source.py", "scripts/.git/source.py")],
], ids=["missing-file", "not-json", "not-object", "missing-key", "boolean", "integer", "string",
        "array", "missing-path", "null-path", "integer-path", "absolute", "empty", "parent",
        "parent-prefix", "parent-component", "dot", "git", "git-prefix", "git-component"])
def test_w10c_production_source_malformed_config_refuses(tmp_path: Path, payload: str | None) -> None:
    skill = tmp_path / "skill"
    config = skill / "config/free_space.json"
    if payload is not None:
        _write(config, payload)
    with pytest.raises(matrix.MatrixError, match="free-space config") as exc:
        matrix._production_source(skill)
    assert str(config) in str(exc.value)


def test_kill_matrix_copy_hook_executable_hashed_and_mutatable(tmp_path: Path, monkeypatch) -> None:
    repo, skill, _ = _tiny(tmp_path)
    target = tmp_path / "copied-repo"
    matrix._copy(skill, repo, target)
    hook = target / matrix.HOOK_REL
    assert hook.read_bytes() == (repo / matrix.HOOK_REL).read_bytes()
    assert hook.stat().st_mode & 0o111 == 0o111
    _write(skill / "tests/test_hook.py", (
        "import subprocess\nfrom pathlib import Path\n\n"
        "def test_hook():\n"
        "    hook = Path(__file__).resolve().parents[4] / 'scripts/git-hooks/pre-commit'\n"
        "    assert subprocess.run([str(hook)]).returncode == 0\n"
    ))
    mutant = {"file": matrix.HOOK_REL.as_posix(), "old": "exit 0", "new": "exit 1",
              "expected_killers": [".claude/skills/minsky/tests/test_hook.py::test_hook"]}
    root = tmp_path / "matrix-temp"
    root.mkdir()
    result = matrix._trial(skill, repo, root, mutant)
    assert result == {"status": "killed", "killing_tests": [
        ".claude/skills/minsky/tests/test_hook.py::test_hook",
    ]}
    assert list(root.iterdir()) == []
    original_hash = matrix.hashlib.sha256

    class WrongHash:
        def digest(self):
            return b"mismatched fixture copy"

    # Return a mismatching digest for the destination read only.
    reads = iter([original_hash(hook.read_bytes()), WrongHash()])
    monkeypatch.setattr(matrix.hashlib, "sha256", lambda payload: next(reads)
                        if payload == hook.read_bytes() else original_hash(payload))
    with pytest.raises(matrix.MatrixError, match="scripts/git-hooks/pre-commit: sha256 mismatch"):
        matrix._copy(skill, repo, tmp_path / "bad-copy")


def test_w12b_matrix_repository_copies_all_hook_bytes_and_modes(tmp_path: Path) -> None:
    repo, skill, _ = _tiny(tmp_path)
    _write(repo / "scripts/git-hooks/support/readme.txt", "fixture hook support\n").chmod(0o640)
    target = tmp_path / "copied-repo"
    matrix._copy(skill, repo, target)
    hooks = sorted(path for path in (repo / "scripts/git-hooks").rglob("*") if path.is_file())
    for source in hooks:
        copied = target / source.relative_to(repo)
        assert copied.read_bytes() == source.read_bytes()
        assert copied.stat().st_mode & 0o7777 == source.stat().st_mode & 0o7777
    expected = {source.relative_to(repo).as_posix():
                (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mode & 0o7777)
                for source in hooks}
    _write(skill / "tests/test_hooks.py", (
        "import hashlib\nimport subprocess\nfrom pathlib import Path\n\n"
        "def test_hooks():\n"
        "    repo = Path(__file__).resolve().parents[4]\n"
        f"    expected = {expected!r}\n"
        "    for relative, (sha, mode) in expected.items():\n"
        "        hook = repo / relative\n"
        "        assert hashlib.sha256(hook.read_bytes()).hexdigest() == sha\n"
        "        assert hook.stat().st_mode & 0o7777 == mode\n"
        "        assert subprocess.run(['git', '--no-optional-locks', '-C', str(repo), 'ls-files', "
        "'--error-unmatch', '--', relative], capture_output=True).returncode == 0\n"
        "    for name in ('pre-commit', 'pre-merge-commit'):\n"
        "        hook = repo / 'scripts/git-hooks' / name\n"
        "        assert hook.stat().st_mode & 0o111\n"
        "        assert subprocess.run([str(hook)]).returncode == 0\n"
    ))
    root = tmp_path / "matrix-temp"
    root.mkdir()
    result = matrix._trial(skill, repo, root, None)
    assert result == {"status": "survived", "killing_tests": [], "tests_passed": 2, "tests_skipped": 0}
    assert list(root.iterdir()) == []


def test_kill_matrix_children_use_nested_roots_and_disable_bytecode(tmp_path: Path, monkeypatch) -> None:
    repo, skill, _ = _tiny(tmp_path)
    root = tmp_path / "matrix-temp"
    root.mkdir()
    run = matrix.subprocess.run
    calls = []

    def capture(argv, **kwargs):
        if argv[:3] == [matrix.PY, "-m", "pytest"]:
            calls.append((argv, kwargs))
        return run(argv, **kwargs)

    monkeypatch.setattr(matrix.subprocess, "run", capture)
    assert matrix._trial(skill, repo, root, None)["status"] == "survived"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    child = Path(kwargs["env"]["MINSKY_TEST_TMP"])
    assert child.name == "child-0" and child.is_relative_to(root)
    assert f"--basetemp={child / 'bt'}" in argv
    assert "-x" not in argv
    assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("intended_kill", [True, False])
def test_kill_matrix_confirms_expected_killer_after_earlier_failure(tmp_path: Path, monkeypatch,
                                                                 intended_kill: bool) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    _write(skill / "tests/test_early.py", (
        "import pytest\nfrom test_probe import probe\n\n"
        "@pytest.fixture\ndef early():\n"
        "    if (probe.VALUE, probe.UNUSED) != (1, 0):\n"
        "        raise RuntimeError('early fixture error')\n\n"
        "def test_early(early):\n    pass\n"
    ))
    root = tmp_path / "matrix-temp"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    out = tmp_path / "matrix.json"
    selected = "T2-killed" if intended_kill else "T3-survived"
    assert matrix.run_matrix(mutants, out, only=[selected], skill_dir=skill, repo_root=repo) == (
        0 if intended_kill else 1)
    row = json.loads(out.read_text())["mutants"][0]
    assert row["status"] == ("killed" if intended_kill else "killed-unexpected")
    assert row["expected_killer"] is intended_kill
    assert row["killing_tests"] == ([".claude/skills/minsky/tests/test_probe.py::test_value"]
                                    if intended_kill else [".claude/skills/minsky/tests/test_early.py::test_early"])
    assert list(root.iterdir()) == []


def test_kill_matrix_expected_trials_report_all_failures_and_errors(tmp_path: Path, monkeypatch) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    _write(skill / "tests/test_errors.py", (
        "import pytest\nfrom test_probe import probe\n\n"
        "@pytest.fixture\ndef setup_error():\n"
        "    if probe.VALUE != 1:\n        raise RuntimeError('fixture setup')\n\n"
        "@pytest.fixture\ndef teardown_error():\n"
        "    yield\n    if probe.VALUE != 1:\n        raise RuntimeError('fixture teardown')\n\n"
        "def test_setup(setup_error):\n    pass\n\n"
        "def test_teardown(teardown_error):\n    pass\n\n"
        "def test_second_failure():\n    assert probe.VALUE == 1\n"
    ))
    mutant = json.loads(mutants.read_text())[0]
    mutant["expected_killers"].append(".claude/skills/minsky/tests/test_errors.py::test_")
    root = tmp_path / "matrix-temp"
    root.mkdir()
    run = matrix.subprocess.run
    calls = []

    def capture(argv, **kwargs):
        if argv[:3] == [matrix.PY, "-m", "pytest"]:
            calls.append(argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(matrix.subprocess, "run", capture)
    result = matrix._trial(skill, repo, root, mutant)
    assert result == {"status": "killed", "killing_tests": [
        ".claude/skills/minsky/tests/test_errors.py::test_second_failure",
        ".claude/skills/minsky/tests/test_errors.py::test_setup",
        ".claude/skills/minsky/tests/test_errors.py::test_teardown",
        ".claude/skills/minsky/tests/test_probe.py::test_value",
    ]}
    assert len(calls) == 2 and "--collect-only" in calls[0]
    assert all("-x" not in argv for argv in calls)
    assert calls[1][3:7] == [
        ".claude/skills/minsky/tests/test_errors.py::test_second_failure",
        ".claude/skills/minsky/tests/test_errors.py::test_setup",
        ".claude/skills/minsky/tests/test_errors.py::test_teardown",
        ".claude/skills/minsky/tests/test_probe.py::test_value",
    ]
    assert list(root.iterdir()) == []


def test_kill_matrix_fallback_collection_error_stays_invalid(tmp_path: Path) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    _write(skill / "tests/test_unexpected.py", (
        "from test_probe import probe\n"
        "if probe.UNUSED:\n    raise RuntimeError('fallback collection error')\n\n"
        "def test_unexpected():\n    pass\n"
    ))
    root = tmp_path / "matrix-temp"
    root.mkdir()
    result = matrix._trial(skill, repo, root, json.loads(mutants.read_text())[1])
    assert result == {"status": "invalid-mutant", "killing_tests": [], "reason": "pytest collection failed"}
    assert list(root.iterdir()) == []


def test_kill_matrix_missing_expected_killer_stays_invalid(tmp_path: Path) -> None:
    repo, skill, mutants = _tiny(tmp_path)
    mutant = json.loads(mutants.read_text())[0]
    missing = ".claude/skills/minsky/tests/test_probe.py::test_missing"
    mutant["expected_killers"] = [missing]
    root = tmp_path / "matrix-temp"
    root.mkdir()
    result = matrix._trial(skill, repo, root, mutant)
    assert result == {"status": "invalid-mutant", "killing_tests": [],
                      "reason": f"expected killers collected no tests: {[missing]}"}
    assert list(root.iterdir()) == []
    mutant["expected_killers"].append(".claude/skills/minsky/tests/test_probe.py::test_value")
    assert matrix._trial(skill, repo, root, mutant) == {"status": "killed", "killing_tests": [
        ".claude/skills/minsky/tests/test_probe.py::test_value",
    ]}
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("tool", ["regression.py", "kill_matrix.py"])
def test_w9_cli_imports_leave_repository_lstat_identical(tmp_path: Path, tool: str) -> None:
    repo = tmp_path / "copied-repo"
    copied = matrix._copy(matrix.SKILL_DIR, matrix.REPO_ROOT, repo)
    before = regression.lstat_census(repo)
    env = {**os.environ}
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run([matrix.PY, str(copied / "tests/tools" / tool), "--help"],
                            cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert regression.lstat_census(repo) == before
