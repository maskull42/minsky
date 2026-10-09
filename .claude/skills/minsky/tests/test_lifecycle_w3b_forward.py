"""W3b forward-rule regressions using only pytest fixture repositories and inert shells."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import jsonschema
import pytest

import worktree_guard
from test_hardening import (
    MODELS, PERSONA, SCRIPTS, STEP_MODELS, load_script, provenance, register_round,
)


launcher = load_script("w3b_launch_chain", "launch-chain.py")


def _git(repo: Path, *args: str) -> str:
    # Git writes are confined to the temporary repositories required by T15/T24.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}
    for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(key, None)
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo),
         "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "user.name=Minsky fixture", "-c", "user.email=fixture@invalid",
         *args], env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("MINSKY_ALLOW_WORKTREE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    for name in ("kept.txt", "modify.txt", "before name.txt",
                 "codex-audits/fixture.txt", ".minsky/fixture.txt"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture {name}\n", encoding="utf-8")
    _git(root, "add", "--", ".")
    _git(root, "commit", "--quiet", "-m", "KB fixture")
    return root


def _linked(repo: Path, *, sparse: bool = False) -> Path:
    path = repo.parent / ("sparse" if sparse else "linked")
    args = ["worktree", "add", "--quiet", "-b", path.name]
    if sparse:
        args.append("--no-checkout")
    _git(repo, *args, str(path))
    if sparse:
        _git(path, "sparse-checkout", "set", "--no-cone", "/kept.txt")
        _git(path, "read-tree", "-mu", "HEAD")
    return path


def _assert_main(module, root: Path, cwd: Path) -> None:
    result = module.check_not_linked_worktree(cwd)
    assert result == {
        "status": "main-checkout", "git_dir": str((root / ".git").resolve()),
        "git_common_dir": str((root / ".git").resolve()), "toplevel": str(root.resolve()),
    }


def test_worktree_main_root_nested_and_scratch(repo: Path, tmp_path: Path) -> None:
    nested = repo / "nested" / "deeper"
    nested.mkdir(parents=True)
    _assert_main(worktree_guard, repo, repo)
    _assert_main(worktree_guard, repo, nested)
    assert worktree_guard.check_not_linked_worktree(tmp_path) == {"status": "not-a-git-work-tree"}


def test_worktree_bare_repository_is_not_a_work_tree(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "--quiet", "--bare")
    assert worktree_guard.check_not_linked_worktree(bare) == {
        "status": "not-a-git-work-tree", "git_dir": str(bare.resolve()),
        "git_common_dir": str(bare.resolve()),
    }
    assert provenance.git_state(bare) == {
        "git_status": "not-a-git-work-tree", "git_head": None, "dirty_paths": None,
    }


def test_worktree_unresolved_strings_mutant_is_killed(repo: Path, tmp_path: Path) -> None:
    nested = repo / "nested" / "deeper"
    nested.mkdir(parents=True)
    source = (SCRIPTS / "worktree_guard.py").read_text(encoding="utf-8")
    mutant = source.replace(
        'git("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")',
        'git("rev-parse", "--git-dir", "--git-common-dir")',
    ).replace(
        'len(lines) != 2 or any(not Path(line).is_absolute() for line in lines)',
        'len(lines) != 2',
    ).replace('str(Path(lines[0]).resolve())', 'lines[0]').replace(
        'str(Path(lines[1]).resolve())', 'lines[1]',
    )
    assert mutant != source
    path = tmp_path / "unresolved_guard.py"
    path.write_text(mutant, encoding="utf-8")
    module = load_script("w3b_unresolved_guard", str(path))
    with pytest.raises((AssertionError, module.WorktreeRefused)):
        _assert_main(module, repo, nested)


def test_worktree_materialised_refusal_names_conditions(repo: Path) -> None:
    linked = _linked(repo)
    with pytest.raises(worktree_guard.WorktreeRefused) as exc:
        worktree_guard.check_not_linked_worktree(linked)
    message = str(exc.value)
    assert str(repo / ".git") in message
    assert str(repo / ".git" / "worktrees" / linked.name) in message
    assert "skip-worktree" in message
    assert str(linked / "codex-audits") in message and str(linked / ".minsky") in message


def test_worktree_absence_without_skip_bits_is_refused(repo: Path) -> None:
    linked = _linked(repo)
    for name in ("codex-audits", ".minsky"):
        shutil.rmtree(linked / name)
    with pytest.raises(worktree_guard.WorktreeRefused, match="skip-worktree tag is not S"):
        worktree_guard.check_not_linked_worktree(linked)


@pytest.mark.parametrize("name", ["codex-audits", ".minsky"])
def test_worktree_sparse_and_dangling_symlink_refusal(repo: Path, name: str) -> None:
    linked = _linked(repo, sparse=True)
    result = worktree_guard.check_not_linked_worktree(linked)
    assert result["status"] == "linked-worktree-sparse-ok"
    assert result["toplevel"] == str(linked.resolve())
    assert result["git_dir"] != result["git_common_dir"]
    (linked / name).symlink_to(linked / "absent-target", target_is_directory=True)
    with pytest.raises(worktree_guard.WorktreeRefused) as exc:
        worktree_guard.check_not_linked_worktree(linked)
    assert str(linked / name) in str(exc.value) and "is present" in str(exc.value)


def test_worktree_override_is_explicit(repo: Path, monkeypatch) -> None:
    linked = _linked(repo)
    monkeypatch.setenv("MINSKY_ALLOW_WORKTREE", "true")
    with pytest.raises(worktree_guard.WorktreeRefused):
        worktree_guard.check_not_linked_worktree(linked)
    monkeypatch.setenv("MINSKY_ALLOW_WORKTREE", "1")
    result = worktree_guard.check_not_linked_worktree(linked)
    assert result["status"] == "linked-worktree-override"
    assert result["git_common_dir"] == str((repo / ".git").resolve())


def test_audit_db_refuses_cwd_before_creating_database(repo: Path, tmp_path: Path) -> None:
    linked = _linked(repo)
    db = tmp_path / "refused.sqlite"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "audit-db.py"), "--db", str(db), "open",
         "--audit-id", "w3b-refused", "--branch", "fixture", "--commit", "0" * 40,
         "--mode", "audit", "--scope", "explicit", "--files", '["kept.txt"]',
         "--personas", json.dumps([PERSONA]), "--models", json.dumps(MODELS)],
        cwd=linked, env={**os.environ, "MINSKY_PROGRESS_ROOT": str(tmp_path / "events")},
        text=True, capture_output=True,
    )
    assert result.returncode == 1 and "refused linked worktree" in result.stderr
    assert not db.exists() and not (tmp_path / "events").exists()


def test_audit_db_refuses_skill_repository_before_writing(repo: Path, tmp_path: Path, monkeypatch) -> None:
    linked = _linked(repo)
    helper = load_script("w3b_audit_db", "audit-db.py")
    monkeypatch.setattr(helper, "_SCRIPTS_DIR", linked / ".claude" / "skills" / "minsky" / "scripts")
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "refused.sqlite"
    assert helper.cmd_open(Namespace(db=str(db))) == 1
    assert not db.exists()


def _dirty_fixture(repo: Path) -> dict:
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "modify.txt").write_text("modified tracked bytes\n", encoding="utf-8")
    _git(repo, "mv", "--", "before name.txt", "after\tname.txt")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    return {"git_status": "ok", "git_head": head,
            "dirty_paths": [[" M", "modify.txt"], ["R ", "after\tname.txt", "before name.txt"]]}


def test_git_state_tracks_dirty_and_rename_but_excludes_untracked(repo: Path, tmp_path: Path) -> None:
    expected = _dirty_fixture(repo)
    assert provenance.git_state(repo) == expected
    assert provenance.git_state(tmp_path) == {
        "git_status": "not-a-git-work-tree", "git_head": None, "dirty_paths": None,
    }


def test_git_state_head_only_mutant_is_killed(repo: Path, tmp_path: Path) -> None:
    expected = _dirty_fixture(repo)
    source = (SCRIPTS / "provenance.py").read_text(encoding="utf-8")
    mutant = source.replace('"dirty_paths": sorted(dirty_paths)', '"dirty_paths": []')
    assert mutant != source
    path = tmp_path / "head_only_provenance.py"
    path.write_text(mutant, encoding="utf-8")
    module = load_script("w3b_head_only_provenance", str(path))
    with pytest.raises(AssertionError):
        assert module.git_state(repo) == expected


def _register_argv(round_dir: Path) -> list[str]:
    return [
        "register-round", "--audit-id", "test-audit", "--round", "2",
        "--round-dir", str(round_dir), "--personas", json.dumps([PERSONA]),
        "--models", json.dumps(MODELS), "--step-models", json.dumps(STEP_MODELS),
    ]


def test_register_scope_11_uses_skill_repository_head(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    scope = provenance.load_round_scope(round_dir)
    assert scope["schema_version"] == "1.1" and scope["git_status"] == "ok"
    assert scope["git_head"] == _git(provenance.SKILL_DIR.parents[2], "rev-parse", "HEAD")
    assert scope["dirty_paths"] == provenance.git_state(provenance.SKILL_DIR.parents[2])["dirty_paths"]
    assert scope["pack_sha256"] is None and "worktree" not in scope


def test_register_scope_pack_hash_and_missing_pack(tmp_path: Path) -> None:
    pack = tmp_path / "pack.xml"
    pack.write_bytes(b"<pack>exact fixture bytes</pack>\n")
    link = tmp_path / "pack-link.xml"
    link.symlink_to(pack)
    round_dir = tmp_path / "round-2"
    assert provenance.main([*_register_argv(round_dir), "--pack", str(link)]) == 0
    assert provenance.load_round_scope(round_dir)["pack_sha256"] == hashlib.sha256(pack.read_bytes()).hexdigest()
    refused = tmp_path / "missing-pack-round"
    with pytest.raises(SystemExit):
        provenance.main([*_register_argv(refused), "--pack", str(tmp_path / "missing.xml")])
    assert not refused.exists()


def test_register_scope_keeps_bytes_when_head_changes(tmp_path: Path, monkeypatch) -> None:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    path = round_dir / "round-scope.json"
    before = path.read_bytes()
    state = provenance.git_state(provenance.SKILL_DIR.parents[2])
    monkeypatch.setattr(provenance, "git_state", lambda repo: {**state, "git_head": "f" * 40})
    register_round(round_dir)
    assert path.read_bytes() == before
    with pytest.raises(SystemExit):
        register_round(round_dir, audit_id="conflicting-audit")
    assert path.read_bytes() == before


def test_register_scope_nonrepo_and_legacy_10_load(tmp_path: Path, monkeypatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(provenance, "SKILL_DIR", scratch / ".claude" / "skills" / "minsky")
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    scope = provenance.load_round_scope(round_dir)
    assert {key: scope[key] for key in ("git_status", "git_head", "dirty_paths", "pack_sha256")} == {
        "git_status": "not-a-git-work-tree", "git_head": None, "dirty_paths": None, "pack_sha256": None,
    }
    legacy = {key: value for key, value in scope.items()
              if key not in ("git_status", "git_head", "dirty_paths", "pack_sha256")}
    legacy["schema_version"] = "1.0"
    path = round_dir / "round-scope.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before = path.read_bytes()
    assert provenance.load_round_scope(round_dir) == legacy
    assert path.read_bytes() == before


def test_register_scope_records_override_and_refuses_without_it(repo: Path, tmp_path: Path, monkeypatch) -> None:
    linked = _linked(repo)
    monkeypatch.setattr(provenance, "SKILL_DIR", linked / ".claude" / "skills" / "minsky")
    round_dir = tmp_path / "round-2"
    with pytest.raises(SystemExit) as exc:
        register_round(round_dir)
    assert exc.value.code == 1 and not round_dir.exists()
    monkeypatch.setenv("MINSKY_ALLOW_WORKTREE", "1")
    register_round(round_dir)
    scope = provenance.load_round_scope(round_dir)
    assert scope["git_head"] == _git(linked, "rev-parse", "HEAD")
    assert scope["worktree"] == worktree_guard.check_not_linked_worktree(linked)
    assert scope["worktree"]["status"] == "linked-worktree-override"


@pytest.mark.parametrize("field,value", [
    ("git_status", None), ("git_status", []), ("git_status", "unknown"),
    ("git_head", None), ("git_head", "bad-head"), ("git_head", "g" * 40),
    ("dirty_paths", None), ("dirty_paths", {}), ("dirty_paths", [[" M"]]),
    ("dirty_paths", [[" M", "p", "old", "extra"]]), ("dirty_paths", [[" M", 1]]),
    ("pack_sha256", "bad-sha"), ("pack_sha256", 1),
])
def test_scope_11_refuses_malformed_fields(tmp_path: Path, capsys, field: str, value) -> None:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    path = round_dir / "round-scope.json"
    scope = json.loads(path.read_text(encoding="utf-8"))
    scope[field] = value
    path.write_text(json.dumps(scope), encoding="utf-8")
    with pytest.raises(SystemExit):
        provenance.load_round_scope(round_dir)
    assert field in capsys.readouterr().err


@pytest.mark.parametrize("field", ["git_status", "git_head", "dirty_paths", "pack_sha256"])
def test_scope_11_refuses_missing_fields(tmp_path: Path, capsys, field: str) -> None:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    path = round_dir / "round-scope.json"
    scope = json.loads(path.read_text(encoding="utf-8"))
    del scope[field]
    path.write_text(json.dumps(scope), encoding="utf-8")
    with pytest.raises(SystemExit):
        provenance.load_round_scope(round_dir)
    assert field in capsys.readouterr().err


def _launch_args(tmp_path: Path, **overrides) -> Namespace:
    round_dir = tmp_path / "round ' $ fixture"
    round_dir.mkdir(exist_ok=True)
    pack = tmp_path / "pack ' $.xml"
    pack.write_bytes(b"<pack/>\n")
    return Namespace(
        **{ "audit_id": "w3b-launch", "round": 2, "round_dir": str(round_dir), "pack": str(pack),
            "personas": PERSONA, "models": json.dumps(MODELS), "step_models": json.dumps(STEP_MODELS),
            "codex_model": "gpt-5.6-sol", "codex_effort": "medium", "codex_timeout": None,
            "opencode_model": None, "opencode_variant": None, "opencode_timeout": None,
            "run_label": None, "extra": [], **overrides }
    )


def _launch_cli(args: Namespace) -> list[str]:
    argv = []
    for name, value in vars(args).items():
        if name != "extra" and value is not None:
            argv += ["--" + name.replace("_", "-"), str(value)]
    return [*argv, *args.extra]


@pytest.mark.parametrize("overrides,field", [
    ({"codex_effort": "xhigh"}, "codex"),
    ({"codex_model": "gpt-6.1-sol"}, "codex"),
    ({"opencode_variant": "low"}, "opencode"),
])
def test_launcher_binding_mismatch_refuses_before_popen(tmp_path: Path, monkeypatch, capsys,
                                                       overrides: dict, field: str) -> None:
    args = _launch_args(tmp_path, **overrides)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not launch"))
    assert launcher.main(_launch_cli(args)) == 1
    assert field in capsys.readouterr().err
    assert not list(Path(args.round_dir).iterdir())


@pytest.mark.parametrize("suffix", ["stdout.log", "stderr.log", "exit", "pid", "env.txt"])
@pytest.mark.parametrize("symlink", [False, True])
def test_launcher_existing_target_refuses_without_overwrite(tmp_path: Path, suffix: str, symlink: bool) -> None:
    args = _launch_args(tmp_path)
    target = Path(args.round_dir) / f"chain.{suffix}"
    if symlink:
        target.symlink_to(tmp_path / "absent-target")
    else:
        target.write_bytes(b"existing evidence\n")
    with pytest.raises(ValueError, match="use --run-label") as exc:
        launcher.build_launch(args)
    assert str(target) in str(exc.value)
    assert target.is_symlink() if symlink else target.read_bytes() == b"existing evidence\n"


def test_launcher_label_names_argv_and_environment(tmp_path: Path, monkeypatch) -> None:
    for key in launcher.ENV_KEYS:
        monkeypatch.setenv(key, "inherited")
    monkeypatch.setenv("OPENCODE_EXTRA", "inherited")
    args = _launch_args(tmp_path, run_label="retry-2", codex_timeout=3600,
                        opencode_model="google/gemini-3.8-flash", opencode_variant="high",
                        opencode_timeout=120, extra=["--", "--only-step", "opencode", "--resume-existing"])
    launch = launcher.build_launch(args)
    assert {path.name for path in launch.paths.values()} == {
        "chain-retry-2.stdout.log", "chain-retry-2.stderr.log", "chain-retry-2.exit",
        "chain-retry-2.pid", "chain-retry-2.env.txt",
    }
    assert launch.argv[launch.argv.index("--models") + 1] == ",".join(MODELS)
    assert launch.argv[-3:] == ["--only-step", "opencode", "--resume-existing"]
    assert [launch.env[key] for key in launcher.ENV_KEYS] == [
        "gpt-5.6-sol", "medium", "3600", "google/gemini-3.8-flash", "high", "120",
    ]
    assert "OPENCODE_EXTRA" not in launch.env
    assert launch.env["PATH"].split(os.pathsep)[0] == str(launcher.SKILL_DIR.parents[2] / ".venv" / "bin")
    assert not list(Path(args.round_dir).iterdir())


def test_launcher_unsets_opencode_and_preserves_omitted_codex_timeout(tmp_path: Path, monkeypatch) -> None:
    for key in launcher.ENV_KEYS:
        monkeypatch.setenv(key, "inherited")
    launch = launcher.build_launch(_launch_args(tmp_path))
    assert launch.env["CODEX_TIMEOUT_SECONDS"] == "inherited"
    assert all(key not in launch.env for key in launcher.ENV_KEYS if key.startswith("OPENCODE_"))


@pytest.mark.parametrize("label", ["", "Bad", "with/slash", "has space", "a\nb"])
def test_launcher_refuses_invalid_labels(tmp_path: Path, label: str) -> None:
    with pytest.raises(ValueError, match="--run-label"):
        launcher.build_launch(_launch_args(tmp_path, run_label=label))


@pytest.mark.parametrize("extra", [
    ["--", "--step-models", "{}"], ["--", "--models=other@high"], ["--", "--mod=other@high"],
])
def test_launcher_refuses_scope_overrides_in_extra_args(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(ValueError, match="overriding registered scope"):
        launcher.build_launch(_launch_args(tmp_path, extra=extra))


@pytest.mark.parametrize("opencode", [False, True])
def test_launcher_detached_wrapper_records_actual_env_without_chain(tmp_path: Path, monkeypatch, capsys,
                                                                  opencode: bool) -> None:
    args = _launch_args(tmp_path, codex_timeout=3600)
    if opencode:
        args.opencode_model = "google/gemini-3.8-flash"
        args.opencode_variant = "high"
        args.opencode_timeout = 90
    original_build = launcher.build_launch
    actual = []

    def harmless_build(parsed: Namespace):
        launch = original_build(parsed)
        argv = ["/bin/bash", "-c", "printf 'harmless stdout\\n'; printf 'harmless stderr\\n' >&2; exit 7"]
        env = {**launch.env, "CODEX_EFFORT": "actual-shell-effort"}
        env.pop("CODEX_TIMEOUT_SECONDS", None)
        result = launch._replace(argv=argv, env=env, wrapper=launcher._shell_wrapper(argv, launch.paths))
        actual.append(result)
        return result

    original_popen = subprocess.Popen
    processes = []

    def capture_popen(*argv, **kwargs):
        assert kwargs["start_new_session"] is True
        assert all(kwargs[key] == subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
        process = original_popen(*argv, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(launcher, "build_launch", harmless_build)
    monkeypatch.setattr(launcher.subprocess, "Popen", capture_popen)
    assert launcher.main(_launch_cli(args)) == 0
    assert processes[0].wait(timeout=10) == 0
    assert capsys.readouterr().out == f"launched pid {processes[0].pid}\n"
    launch = actual[0]
    assert launch.paths["pid"].read_text() == f"{processes[0].pid}\n"
    assert launch.paths["exit"].read_text() == "7\n"
    assert launch.paths["stdout"].read_text() == "harmless stdout\n"
    assert launch.paths["stderr"].read_text() == "harmless stderr\n"
    expected = [f"{key}={launch.env.get(key, '<unset>')}" for key in launcher.ENV_KEYS]
    assert launch.paths["env"].read_text() == "\n".join([*expected, "CHAIN_ARGV=" + shlex.join(launch.argv)]) + "\n"


def test_launcher_rechecks_collisions_before_detaching(tmp_path: Path, monkeypatch, capsys) -> None:
    args = _launch_args(tmp_path)
    original_build = launcher.build_launch

    def collide(parsed: Namespace):
        launch = original_build(parsed)
        launch.paths["stdout"].write_bytes(b"concurrent evidence\n")
        return launch

    monkeypatch.setattr(launcher, "build_launch", collide)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not launch"))
    assert launcher.main(_launch_cli(args)) == 1
    assert "use --run-label" in capsys.readouterr().err
    assert (Path(args.round_dir) / "chain.stdout.log").read_bytes() == b"concurrent evidence\n"
    assert not (Path(args.round_dir) / "chain.pid").exists()


@pytest.mark.parametrize("retrospective", [False, True])
def test_round_layout_schema_validates_declared_classes(retrospective: bool) -> None:
    schema = json.loads((SCRIPTS.parent / "schemas" / "round-layout.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    layout = {
        "schema": "minsky-round-layout/1", "raw_directories": [
            {"path": "native-calls", "class": "raw-stream"},
            {"path": "native-calls/state", "class": "native-state"},
            {"path": "native-calls/cache", "class": "dependency-cache"},
        ], "retrospective": retrospective, "written_by": "gpt-6.1-sol@xhigh",
        "written_at": "2026-10-01T10:00:00Z",
    }
    validator.validate(layout)
    for field, value in (("written_at", "2026-10-01T10:00:00+00:00"),
                         ("retrospective", "true"), ("written_by", "")):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate({**layout, field: value})
    for path in ("/absolute", "../escape", "nested/../escape", "./relative", ""):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate({**layout, "raw_directories": [{"path": path, "class": "raw-stream"}]})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**layout, "raw_directories": [{"path": "raw", "class": "other"}]})
